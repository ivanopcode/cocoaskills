"""Portable Windows/Linux launcher admission witnesses and native controls."""
from __future__ import annotations

import itertools
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from csk import mcp_launchers as gate


@pytest.fixture(autouse=True)
def portable_linux_facts(monkeypatch):
    # Pure Linux policy tests also run on Windows, whose stat/chmod has no Unix
    # owner/execute-mode semantics. Native controls live in a separate module.
    if sys.platform == "win32":
        def metadata(path):
            info = path.stat()
            return SimpleNamespace(st_uid=1000, st_mode=(stat.S_IFDIR if stat.S_ISDIR(info.st_mode) else stat.S_IFREG) | 0o755)
        monkeypatch.setattr(gate, "linux_uid", lambda: 1000)
        monkeypatch.setattr(gate, "linux_metadata", metadata)


def windows(root="%LOCALAPPDATA%", allow=True):
    return gate.parse_launcher({"windows": {"install_dir": root + "\\Programs\\Example",
        "executable": "mcp-server.exe", "publisher": "Example Inc",
        **({"trust": "allow-unverified"} if allow else {})}})


def linux(allow=True):
    return gate.parse_launcher({"linux": {"path": "${XDG_CONFIG_HOME:-~/.config}/Example/bin/server",
        **({"trust": "allow-unverified"} if allow else {})}})


def helper(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"launcher fixture")
    path.chmod(0o755)
    return path


# Every forbidden Windows path form refuses the declaration; a contained root
# and executable with the same neighboring spelling remains a positive control.
@pytest.mark.parametrize("field,value", [
    ("install_dir", r"\\server\share\app"), ("install_dir", r"\\?\C:\app"),
    ("install_dir", r"\\.\C:\app"), ("install_dir", r"C:app"),
    ("install_dir", r"%LOCALAPPDATA%\Programs\..\app"),
    ("install_dir", r"%ProgramFiles%\app:stream"),
    ("install_dir", r"%OTHER%\app"), ("install_dir", r"%ProgramFiles%\app. "),
    ("executable", r"..\app.exe"), ("executable", r"app.exe:stream"),
    ("executable", r"\\server\app.exe"), ("executable", r"C:app.exe"),
    ("executable", r"\\?\C:\app.exe"), ("executable", r"NUL.exe"),
])
def test_windows_path_grammar(field, value):
    raw = {"install_dir": r"%ProgramFiles%\Example", "executable": "server.exe", "publisher": "Example Inc"}
    assert gate.parse_launcher({"windows": raw}).windows
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.parse_launcher({"windows": {**raw, field: value}})
    assert caught.value.code == "mcp_declaration_invalid"


# A bad or foreign signature never becomes unsigned; each allowed unsigned or
# matching valid signature is classified by the declared machine/user root.
@pytest.mark.parametrize("root,allow,verdict", list(itertools.product(
    ["%ProgramFiles%", "%ProgramFiles(x86)%", "%LOCALAPPDATA%"], [False, True],
    ["unsigned", "valid", "foreign", "invalid"])))
def test_windows_verdict_matrix(tmp_path, root, allow, verdict):
    path = helper(tmp_path, "Programs/Example/mcp-server.exe")
    checked = SimpleNamespace(signature="valid" if verdict == "foreign" else verdict,
                              publisher="Other Inc" if verdict == "foreign" else "Example Inc")
    call = lambda: gate.resolve_launcher(windows(root, allow), platform="windows", roots=(tmp_path,),
                                         verifier=lambda *args: checked)
    if verdict in {"foreign", "invalid"} or verdict == "unsigned" and not allow:
        with pytest.raises(gate.McpLauncherError) as caught:
            call()
        assert caught.value.code == "mcp_launcher_untrusted"
    else:
        result = call()
        expected = "unverified" if verdict == "unsigned" else "signed-user-writable" if root == "%LOCALAPPDATA%" else "verified"
        assert result.trust == expected and result.path == str(path.resolve())
        assert result.publisher == ("windows:unsigned" if verdict == "unsigned" else "windows:Example Inc")


# Realpath containment rejects both an install-directory escape and a helper
# escape before invoking trust verification; the repaired original passes.
@pytest.mark.parametrize("platform,escape", list(itertools.product(["windows", "linux"], ["directory", "file"])))
def test_realpath_escape(tmp_path, monkeypatch, platform, escape):
    root = tmp_path / "root"
    path = helper(root, "Programs/Example/mcp-server.exe" if platform == "windows" else "Example/bin/server")
    outside = helper(tmp_path / "outside", path.name)
    selected = path.parent if escape == "directory" else path
    saved = selected.with_name(selected.name + "-saved")
    selected.rename(saved)
    selected.symlink_to(outside.parent if escape == "directory" else outside, target_is_directory=escape == "directory")
    spec = windows() if platform == "windows" else linux()
    calls = []
    def verify(*args):
        calls.append(args)
        return SimpleNamespace(signature="valid", publisher="Example Inc", package_owned=False)
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.resolve_launcher(spec, platform=platform, roots=(root,), verifier=verify)
    assert caught.value.code == "mcp_launcher_path_invalid" and not calls
    selected.unlink()
    saved.rename(selected)
    assert gate.resolve_launcher(spec, platform=platform, roots=(root,), verifier=verify).path == str(path.resolve())


# Injected filesystem facts isolate owner/mode policy from the host OS. A
# package result verifies only root-owned files; error/no ownership is unsigned.
@pytest.mark.parametrize("owner,bad_mode,package,allow", list(itertools.product(
    [0, 1000, 2000], [None, "file-group", "parent-world", "root-group", "parent-owner"],
    [True, False, "error"], [False, True])))
def test_linux_verdict_matrix(tmp_path, monkeypatch, owner, bad_mode, package, allow):
    path = helper(tmp_path, "Example/bin/server")
    monkeypatch.setattr(gate, "linux_uid", lambda: 1000, raising=False)
    def metadata(p):
        mode = stat.S_IFREG | 0o755 if p == path else stat.S_IFDIR | 0o755
        uid = owner if p == path else 1000
        if (bad_mode == "file-group" and p == path or bad_mode == "parent-world" and p == path.parent
                or bad_mode == "root-group" and p == tmp_path): mode |= 0o022
        if bad_mode == "parent-owner" and p == path.parent: uid = 2000
        return SimpleNamespace(st_uid=uid, st_mode=mode)
    monkeypatch.setattr(gate, "linux_metadata", metadata, raising=False)
    def verify(*args):
        if package == "error": raise OSError("package probe failed")
        return SimpleNamespace(package_owned=package)
    call = lambda: gate.resolve_launcher(linux(allow), platform="linux", roots=(tmp_path,), verifier=verify)
    unsafe = owner == 2000 or bad_mode is not None
    verified = owner == 0 and package is True
    if unsafe or not verified and not allow:
        with pytest.raises(gate.McpLauncherError) as caught: call()
        assert caught.value.code == ("mcp_launcher_path_invalid" if unsafe else "mcp_launcher_untrusted")
    else:
        resolved = call()
        assert resolved.trust == ("verified" if verified else "unverified")
        assert resolved.publisher == "linux:package-manager" and resolved.path == str(path.resolve())


# Files must be regular and executable independently of the trust override.
@pytest.mark.parametrize("kind", ["directory", "not-executable"])
def test_linux_file_type(tmp_path, monkeypatch, kind):
    path = helper(tmp_path, "Example/bin/server")
    if kind == "directory": path.unlink(); path.mkdir()
    else:
        path.chmod(0o644)
        metadata = gate.linux_metadata
        monkeypatch.setattr(gate, "linux_metadata", lambda p: SimpleNamespace(st_uid=0,
            st_mode=stat.S_IFREG | 0o644) if p == path else metadata(p))
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.resolve_launcher(linux(), platform="linux", roots=(tmp_path,), verifier=lambda *a: SimpleNamespace(package_owned=False))
    assert caught.value.code == "mcp_launcher_path_invalid"


# Only documented roots/expansions parse; spelling a different environment
# expansion or traversal never becomes a runtime lookup.
@pytest.mark.parametrize("path", ["/tmp/server", "${HOME}/server", "${XDG_CONFIG_HOME}/server",
    "${XDG_DATA_HOME}/server", "${XDG_CONFIG_HOME:-~/.config}/../server", "/opt/a//server",
    "/usr/bin/../server", "~/.local/bin/$USER", "/opt/a\\server"])
def test_linux_path_grammar(path):
    assert gate.parse_launcher({"linux": {"path": "/opt/app/server"}}).linux
    with pytest.raises(gate.McpLauncherError) as caught:
        gate.parse_launcher({"linux": {"path": path}})
    assert caught.value.code == "mcp_declaration_invalid"


# Root expansion is literal and bounded: empty XDG values use the documented
# fallback, absolute configured roots work, relative configured roots refuse.
@pytest.mark.parametrize("variable,prefix,fallback", [
    ("XDG_CONFIG_HOME", "${XDG_CONFIG_HOME:-~/.config}/", ".config"),
    ("XDG_DATA_HOME", "${XDG_DATA_HOME:-~/.local/share}/", ".local/share")])
def test_xdg_expansion(tmp_path, monkeypatch, variable, prefix, fallback):
    monkeypatch.setattr(gate.Path, "home", lambda: tmp_path)
    default = tmp_path / fallback
    default.mkdir(parents=True)
    monkeypatch.setenv(variable, "")
    assert gate._linux_root(prefix) == default.resolve()
    custom = tmp_path / "custom $literal"
    custom.mkdir()
    monkeypatch.setenv(variable, str(custom))
    assert gate._linux_root(prefix) == custom.resolve()
    monkeypatch.setenv(variable, "relative/path")
    with pytest.raises(gate.McpLauncherError) as caught: gate._linux_root(prefix)
    assert caught.value.code == "mcp_launcher_path_invalid"


# Fixed package argv/environment reject failed probes and dpkg pattern matches
# for a different pathname; both package tools have successful nearby controls.
@pytest.mark.parametrize("answer", ["dpkg", "rpm", "no", "neighbor", "error", "timeout"])
def test_package_probe_contract(tmp_path, monkeypatch, answer):
    import subprocess
    from pathlib import Path
    path = tmp_path / "literal[abc]"
    programs = {Path("/usr/bin/dpkg"), Path("/usr/bin/rpm")}
    known = {p for tool in programs for p in (tool, *tool.parents)}
    original_stat, original_resolve = Path.stat, Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, **kw: p if p in programs else original_resolve(p, **kw))
    monkeypatch.setattr(Path, "stat", lambda p, **kw: SimpleNamespace(st_uid=0, st_mode=(stat.S_IFREG if p in programs else stat.S_IFDIR) | 0o755) if p in known else original_stat(p, **kw))
    monkeypatch.setattr(gate.os, "access", lambda p, mode: Path(p) in programs)
    monkeypatch.setenv("DPKG_ROOT", str(tmp_path / "hostile-database"))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert Path(argv[0]) in programs
        assert kwargs == dict(capture_output=True, text=True, timeout=5, check=False,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        assert argv[1:] == (["-S", "--", str(path)] if Path(argv[0]).name == "dpkg" else ["-qf", "--", str(path)])
        if answer == "error": raise OSError("probe error")
        if answer == "timeout": raise subprocess.TimeoutExpired(argv, 5)
        success = Path(argv[0]).name == answer
        output = "pkg: " + str(path) if success else "pkg: " + str(tmp_path / "other")
        return SimpleNamespace(returncode=0 if success or answer == "neighbor" and Path(argv[0]).name == "dpkg" else 1, stdout=output)
    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.linux_package_owned(path) is (answer in {"dpkg", "rpm"})
    assert calls


# A verifier that replaces bytes during its observation cannot publish a digest
# for a different file; unchanged bytes are the positive control in the matrix.
@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_content_race_refuses(tmp_path, platform):
    path = helper(tmp_path, "Programs/Example/mcp-server.exe" if platform == "windows" else "Example/bin/server")
    def changed(*args):
        path.write_bytes(b"different bytes during verification")
        return SimpleNamespace(signature="valid", publisher="Example Inc", package_owned=True)
    if platform == "linux":
        # User-owned files do not invoke package tools; changing during the hash
        # is covered by the Windows verifier seam here, and root-owned facts
        # make this lane exercise the same verification/hash boundary.
        from unittest.mock import patch
        metadata = gate.linux_metadata
        with patch.object(gate, "linux_metadata", lambda p: SimpleNamespace(st_mode=metadata(p).st_mode, st_uid=0)):
            with pytest.raises(gate.McpLauncherError) as caught:
                gate.resolve_launcher(linux(), platform=platform, roots=(tmp_path,), verifier=changed)
    else:
        with pytest.raises(gate.McpLauncherError) as caught:
            gate.resolve_launcher(windows(), platform=platform, roots=(tmp_path,), verifier=changed)
    assert caught.value.code == "mcp_launcher_untrusted"


# Real environment values cannot introduce UNC/device/drive-relative roots,
# traversal or ADS; ordinary drive-qualified Program Files remains valid.
@pytest.mark.parametrize("value", [r"\\host\share", r"\\?\C:\Apps", r"\\.\C:\Apps", "C:Apps",
    r"C:\Apps\..\Other", r"C:\Apps:stream", "", "relative", "C:\\Apps\\\n"])
def test_windows_environment_root(value):
    assert gate._windows_root_value(r"C:\Program Files") == r"C:\Program Files"
    with pytest.raises(gate.McpLauncherError) as caught: gate._windows_root_value(value)
    assert caught.value.code == "mcp_launcher_path_invalid"
