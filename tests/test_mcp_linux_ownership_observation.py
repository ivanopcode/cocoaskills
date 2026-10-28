"""Portable observation controls; package tools and kernel ownership are modeled."""
from pathlib import Path
from types import SimpleNamespace
import os
import stat
import subprocess

import pytest

from csk import mcp_launchers as gate


@pytest.fixture
def package_model(tmp_path, monkeypatch):
    root = tmp_path / "root"
    file = root / "bin/PATH_SECRET_CANARY"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"fixture bytes, never executed")
    settings = SimpleNamespace(available={"dpkg"}, answers={}, owner=0, tool_uid=0,
                               tool_mode=0o755, executable=True, tool_is_file=True,
                               writable_parent=False)
    calls = []
    programs = {"/usr/bin/dpkg", "/usr/bin/rpm"}

    class ToolPath:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return self.value

        def resolve(self, **kwargs):
            if self.value.rsplit("/", 1)[-1] not in settings.available:
                raise FileNotFoundError("MISSING_SECRET_CANARY")
            return self

        @property
        def parents(self):
            return tuple(ToolPath(p) for p in ("/usr/bin", "/usr", "/"))

        def stat(self):
            tool = self.value in programs
            return SimpleNamespace(st_uid=settings.tool_uid if tool else 0,
                                   st_mode=(stat.S_IFREG if tool and settings.tool_is_file else stat.S_IFDIR)
                                   | (settings.tool_mode if tool else 0o755))

        def is_file(self):
            return settings.tool_is_file

    class LocalOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def access(self, path, mode):
            if isinstance(path, ToolPath):
                return settings.executable
            return True if path == file else os.access(path, mode)

    def run(argv, **kwargs):
        assert argv[0] in programs
        assert kwargs == dict(capture_output=True, text=True, timeout=5, check=False,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        backend = argv[0].rsplit("/", 1)[-1]
        assert argv[1:3] == (["-S", "--"] if backend == "dpkg" else ["-qf", "--"])
        calls.append(argv)
        answer = settings.answers.get(backend, (0, "example: " + argv[-1] + "\n"))
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(argv, answer[0], answer[1], "STDERR_SECRET_CANARY")

    def metadata(path):
        mode = (stat.S_IFREG if path == file else stat.S_IFDIR) | 0o755
        if settings.writable_parent and path == file.parent:
            mode |= 0o020
        return SimpleNamespace(st_uid=settings.owner, st_mode=mode)

    # Replace only module references: unrelated pathlib/os/subprocess callers retain
    # their original objects and cannot be captured by this process/metadata model.
    monkeypatch.setattr(gate, "Path", lambda p: ToolPath(str(p)) if str(p) in programs else Path(p))
    monkeypatch.setattr(gate, "os", LocalOS())
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(gate, "linux_metadata", metadata)
    monkeypatch.setattr(gate, "linux_uid", lambda: 1000)
    return root, file, settings, calls


# A known timeout and an rc=0 no-exact-path record must yield distinct safe reasons
# through the same real resolver call; both refuse, while adjacent ownership verifies.
@pytest.mark.parametrize("outcome", ["timeout", "no-exact-match", "owned"])
def test_refusal_observes_same_attempt(package_model, outcome):
    root, file, settings, calls = package_model
    if outcome == "timeout":
        settings.answers["dpkg"] = subprocess.TimeoutExpired(
            "COMMAND_SECRET_CANARY", 5, output="OUTPUT_SECRET_CANARY", stderr="STDERR_SECRET_CANARY")
    elif outcome == "no-exact-match":
        settings.answers["dpkg"] = (0, "example: /other/OUTPUT_SECRET_CANARY\n")
    declaration = gate.parse_launcher({"linux": {"path": "/usr/bin/bin/" + file.name}})
    before = file.read_bytes()
    if outcome == "owned":
        assert gate.resolve_launcher(declaration, platform="linux", roots=(root,)).trust == "verified"
    else:
        with pytest.raises(gate.McpLauncherError) as caught:
            gate.resolve_launcher(declaration, platform="linux", roots=(root,))
        assert caught.value.code == "mcp_launcher_untrusted"
        assert f"dpkg={outcome}, rpm=tool-missing" in str(caught.value)
        assert "SECRET_CANARY" not in str(caught.value)
    assert calls == [["/usr/bin/dpkg", "-S", "--", str(file)]]
    assert file.read_bytes() == before


# Failed/malformed/mismatched records preserve the bool result and are summarized
# without raw streams, exception text or paths. Neither observation nor bool API requery.
@pytest.mark.parametrize("case,reason", [
    ("nonzero", "nonzero"), ("malformed", "no-exact-match"), ("mismatch", "no-exact-match"),
    ("oserror", "os-error"), ("decode", "decode-error"), ("value", "value-error"),
])
def test_package_failure_observation(package_model, case, reason):
    _, file, settings, calls = package_model
    settings.answers["dpkg"] = {
        "nonzero": (1, "example: " + str(file)),
        "malformed": (0, "malformed OUTPUT_SECRET_CANARY"),
        "mismatch": (0, "example: " + str(file) + ".other"),
        "oserror": OSError("ERROR_SECRET_CANARY"),
        "decode": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "ERROR_SECRET_CANARY"),
        "value": ValueError("ERROR_SECRET_CANARY"),
    }[case]
    observed = gate._linux_package_observation(file)
    assert observed.owned is False
    assert observed.summary() == f"dpkg={reason}, rpm=tool-missing"
    assert len(calls) == 1


# An earlier failed backend stays visible when the existing trusted rpm fallback
# succeeds. Successful observation and legacy bool API each use one pass, not a retry.
def test_successful_fallback_and_bool_api(package_model):
    _, file, settings, calls = package_model
    settings.available.add("rpm")
    settings.answers["dpkg"] = subprocess.TimeoutExpired("secret", 5)
    observed = gate._linux_package_observation(file)
    assert observed.owned is True
    assert observed.summary() == "dpkg=timeout, rpm=owned"
    assert [argv[0] for argv in calls] == ["/usr/bin/dpkg", "/usr/bin/rpm"]
    calls.clear()
    assert gate.linux_package_owned(file) is True
    assert [argv[0] for argv in calls] == ["/usr/bin/dpkg", "/usr/bin/rpm"]


# Tool validation and skipped nonroot ownership are distinct, with no process
# started on these paths. Opt-in remains unverified; an unsafe chain still refuses.
@pytest.mark.parametrize("case,reason", [("missing", "tool-missing"), ("untrusted", "tool-untrusted"),
                                        ("writable", "tool-untrusted"), ("nonexec", "tool-not-executable"),
                                        ("directory", "tool-not-executable")])
def test_tool_validation_observation(package_model, case, reason):
    _, file, settings, calls = package_model
    if case == "missing":
        settings.available.clear()
    elif case == "untrusted":
        settings.tool_uid = 1000
    elif case == "writable":
        settings.tool_mode = 0o777
    elif case == "directory":
        settings.tool_is_file = False
    else:
        settings.executable = False
    observed = gate._linux_package_observation(file)
    assert observed.owned is False
    assert observed.summary() == f"dpkg={reason}, rpm=tool-missing"
    assert not calls


def test_nonroot_skip_opt_in_and_unsafe_chain(package_model):
    root, file, settings, calls = package_model
    settings.owner = 1000
    raw = {"path": "/usr/bin/bin/" + file.name}
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.resolve_launcher(gate.parse_launcher({"linux": raw}), platform="linux", roots=(root,))
    assert caught.value.code == "mcp_launcher_untrusted"
    assert "skipped-nonroot" in str(caught.value)
    raw["trust"] = "allow-unverified"
    spec = gate.parse_launcher({"linux": raw})
    assert gate.resolve_launcher(spec, platform="linux", roots=(root,)).trust == "unverified"
    settings.writable_parent = True
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.resolve_launcher(spec, platform="linux", roots=(root,))
    assert caught.value.code == "mcp_launcher_path_invalid"
    assert not calls


# The new test seams do not mutate shared stdlib attributes; capture is confined
# to package calls made by this module, not pytest or unrelated subprocess users.
def test_model_is_module_local(package_model):
    assert gate.Path is not Path and gate.os is not os and gate.subprocess is not subprocess
    assert subprocess.run.__module__ == "subprocess"
    assert Path.__module__ == "pathlib"
    assert os.access.__module__ in {"posix", "nt"}


# Unexpected exceptions retain their identity and stop this attempt: observation
# must neither turn them into ownership nor query the next backend to hide them.
def test_observation_does_not_replace_unhandled_exception(package_model):
    root, file, settings, calls = package_model
    failure = LookupError("ERROR_SECRET_CANARY")
    settings.answers["dpkg"] = failure
    settings.available.add("rpm")
    spec = gate.parse_launcher({"linux": {"path": "/usr/bin/bin/" + file.name}})
    with pytest.raises(LookupError) as caught:
        gate.resolve_launcher(spec, platform="linux", roots=(root,))
    assert caught.value is failure
    assert calls == [["/usr/bin/dpkg", "-S", "--", str(file)]]
