"""App launcher grammar, identity admission and native signature controls."""
from __future__ import annotations

import itertools
import plistlib
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from csk import mcp_declarations as declarations

MAC = {"bundle_id": "com.example.App", "team_id": "ABCDE12345", "executable": "Contents/MacOS/mcp-server"}
WINDOWS = {"install_dir": "%LOCALAPPDATA%\\Programs\\Example", "executable": "mcp-server.exe", "publisher": "Example Inc", "trust": "allow-unverified"}
LINUX = {"path": "${XDG_CONFIG_HOME:-~/.config}/Example/bin/mcp-server", "trust": "allow-unverified"}


def declaration(launcher=None):
    return {"app": {"server": {"transport": "stdio", "launcher": launcher or {"macos": dict(MAC)}, "args": []}}}


def parse(raw):
    return declarations.parse_mcp_servers(raw, invocation_scope="project", target_agents=["claude_code", "codex_cli", "cursor"])


# All three portable entries parse together; resolution is a separate host operation.
def test_launcher_grammar_positive_control():
    spec = parse(declaration({"macos": MAC, "windows": WINDOWS, "linux": LINUX}))["app"].server
    assert spec.command is None
    assert spec.launcher.macos.bundle_id == MAC["bundle_id"]
    assert spec.launcher.windows.install_dir == WINDOWS["install_dir"]
    assert spec.launcher.linux.path == LINUX["path"]


# Closed platform fields, known roots/placeholders and contained relative paths reject these exact invalid forms.
@pytest.mark.parametrize("platform,field,value", [
    ("macos", "trust", "allow-unverified"), ("macos", "team_id", "bad team"),
    ("macos", "bundle_id", 'com.example.App" or true'),
    ("macos", "executable", "../escape"), ("macos", "executable", "/Applications/a"),
    ("macos", "executable", "Contents/../escape"), ("macos", "executable", "Contents\\MacOS\\x"),
    ("macos", "executable", "Contents/MacOS/"),
    ("windows", "install_dir", "C:\\Temp\\Example"),
    ("windows", "install_dir", "%UNKNOWN%\\Example"),
    ("windows", "install_dir", "%LOCALAPPDATA%\\Programs\\..\\Other"),
    ("windows", "install_dir", "%LOCALAPPDATA%\\Programs\\%TEMP%"),
    ("windows", "executable", "..\\mcp.exe"), ("windows", "executable", "C:\\mcp.exe"),
    ("windows", "executable", "mcp.cmd"), ("windows", "publisher", ""),
    ("windows", "trust", "verified"), ("windows", "extra", True),
    ("linux", "path", "/tmp/server"), ("linux", "path", "${HOME}/bin/server"),
    ("linux", "path", "${XDG_CONFIG_HOME:-~/.config}/../server"),
    ("linux", "path", "~/.local/bin/${SECRET}"), ("linux", "path", "/opt/../tmp/server"),
    ("linux", "trust", "always"), ("linux", "path", "~/.local/bin/a\n"),
])
def test_invalid_platform_entry(platform, field, value):
    entry = dict({"macos": MAC, "windows": WINDOWS, "linux": LINUX}[platform])
    entry[field] = value
    with pytest.raises(declarations.McpDeclarationError) as caught:
        parse(declaration({platform: entry}))
    assert caught.value.code == "mcp_declaration_invalid"
    assert ".launcher." + platform in caught.value.field


# command/launcher are exclusive, stdio-only, and platform maps must be nonempty and closed.
@pytest.mark.parametrize("mutation", ["both", "neither", "empty", "unknown", "http", "wrong-type"])
def test_invalid_launcher_combination(mutation):
    raw = declaration()
    server = raw["app"]["server"]
    if mutation == "both": server["command"] = "server"
    elif mutation == "neither": del server["launcher"]
    elif mutation == "empty": server["launcher"] = {}
    elif mutation == "unknown": server["launcher"] = {"darwin": MAC}
    elif mutation == "http": server.update(transport="http", url="https://example.invalid/mcp")
    else: server["launcher"] = []
    with pytest.raises(declarations.McpDeclarationError) as caught:
        parse(raw)
    assert caught.value.code == "mcp_declaration_invalid"


def bundle(root: Path):
    app = root / "Example.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": MAC["bundle_id"]}))
    helper = app / MAC["executable"]
    helper.write_bytes(b"native fixture")
    helper.chmod(0o755)
    return app, helper


def good_verification():
    from csk import mcp_launchers as launchers
    return launchers.MacOSVerification(True, True, MAC["team_id"], MAC["bundle_id"], "mcp-server")


# Every failure dimension refuses with its exact code; only a found, contained, valid and correctly identified notarized helper passes.
@pytest.mark.parametrize("found,escape,signature,team,identifier,notarized", list(itertools.product([False, True], repeat=6)))
def test_resolution_verdict_table(tmp_path, found, escape, signature, team, identifier, notarized):
    from csk import mcp_launchers as launchers
    app, helper = bundle(tmp_path)
    if not found: (app / "Contents/Info.plist").unlink()
    if escape:
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        outside.chmod(0o755)
        helper.unlink()
        helper.symlink_to(outside)
    verification = replace(good_verification(), signature_valid=signature, notarized=notarized,
                           team_id=MAC["team_id"] if team else "WRONG12345",
                           identifier="mcp-server" if identifier else "other")
    calls = []
    def verify(*args):
        calls.append(args)
        return verification
    kwargs = dict(platform="macos", roots=(tmp_path,), verifier=verify)
    spec = parse(declaration())["app"].server.launcher
    expected = ("mcp_launcher_not_found" if not found else "mcp_launcher_path_invalid" if escape
                else "mcp_launcher_untrusted" if not all((signature, team, identifier, notarized)) else None)
    if expected:
        with pytest.raises(launchers.McpLauncherError) as caught:
            launchers.resolve_launcher(spec, **kwargs)
        assert caught.value.code == expected
        assert bool(calls) == (found and not escape)
    else:
        result = launchers.resolve_launcher(spec, **kwargs)
        assert result.path == str(helper.resolve())
        assert result.trust == "verified"
        assert MAC["team_id"] in result.publisher
        assert len(result.content_sha256) == 64
    assert (tmp_path / "outside").read_bytes() == b"outside" if escape else helper.read_bytes() == b"native fixture"


# Missing executables and absent current-platform entries have distinct refusals before invoking a verifier.
@pytest.mark.parametrize("platform,entries,code", [
    ("windows", {"windows": WINDOWS}, "mcp_launcher_path_invalid"),
    ("linux", {"linux": LINUX}, "mcp_launcher_path_invalid"),
    ("macos", {"linux": LINUX}, "mcp_launcher_platform_missing"),
    ("windows", {"macos": MAC}, "mcp_launcher_platform_missing"),
])
def test_platform_refusal(tmp_path, platform, entries, code):
    from csk import mcp_launchers as launchers
    spec = parse(declaration(entries))["app"].server.launcher
    with pytest.raises(launchers.McpLauncherError) as caught:
        launchers.resolve_launcher(spec, platform=platform, roots=(tmp_path,), verifier=lambda *args: pytest.fail("verification must not run"))
    assert caught.value.code == code


# Duplicate bundle IDs refuse ambiguity rather than silently changing the selected app; aliases of one bundle do not conflict.
def test_ambiguous_bundle_and_alias(tmp_path):
    from csk import mcp_launchers as launchers
    first, _ = bundle(tmp_path / "one")
    second, _ = bundle(tmp_path / "two")
    spec = parse(declaration())["app"].server.launcher
    with pytest.raises(launchers.McpLauncherError, match="mcp_launcher_ambiguous"):
        launchers.resolve_launcher(spec, platform="macos", roots=(first.parent, second.parent), verifier=lambda *a: good_verification())
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / "Example.app").symlink_to(first, target_is_directory=True)
    resolved = launchers.resolve_launcher(spec, platform="macos", roots=(first.parent, alias), verifier=lambda *a: good_verification())
    assert resolved.path == str((first / MAC["executable"]).resolve())


# The native verifier uses only the system codesign, strict whole-bundle validation, and explicit helper/publisher/notarization requirements; any command failure refuses.
@pytest.mark.parametrize("fail_at", [None, 0, 1])
def test_codesign_requirements(monkeypatch, tmp_path, fail_at):
    from csk import mcp_launchers as launchers
    import subprocess
    app, helper = bundle(tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, int(len(calls) - 1 == fail_at), stdout="", stderr="")
    monkeypatch.setattr(launchers.subprocess, "run", run)
    spec = parse(declaration())["app"].server.launcher.macos
    if fail_at is not None:
        with pytest.raises(launchers.McpLauncherError) as caught:
            launchers.verify_macos(app, helper, spec)
        assert caught.value.code == "mcp_launcher_untrusted"
        assert len(calls) == fail_at + 1
    else:
        result = launchers.verify_macos(app, helper, spec)
        assert result == good_verification()
        assert len(calls) == 2
    assert calls[0][0] == "/usr/bin/codesign"
    assert "--strict" in calls[0] and "--deep" in calls[0]
    assert "--all-architectures" in calls[0]
    assert all('notarized' in ' '.join(c) and 'anchor apple generic' in ' '.join(c) and MAC["team_id"] in ' '.join(c) for c in calls)
    if len(calls) == 2: assert 'identifier "mcp-server"' in ' '.join(calls[1])


# On a real macOS Developer-ID app, the production verifier accepts the original and rejects a copy whose sealed helper bytes changed; the app is never launched.
@pytest.mark.csk_e2e_native
@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS signature check")
def test_native_proxyman_signature_and_tamper(tmp_path):
    from csk import mcp_launchers as launchers
    app = Path("/Applications/Proxyman.app")
    if not app.is_dir(): pytest.skip("Proxyman is required for the native positive control")
    spec = parse(declaration({"macos": {"bundle_id": "com.proxyman.NSProxy", "team_id": "3X57WP8E8V", "executable": "Contents/MacOS/mcp-server"}}))["app"].server.launcher
    original = launchers.resolve_launcher(spec, platform="macos", roots=(app.parent,))
    assert original.trust == "verified"
    assert original.path == str(app / "Contents/MacOS/mcp-server")
    copied_root = tmp_path / "copied"
    copied_root.mkdir()
    copy = copied_root / "Proxyman.app"
    shutil.copytree(app, copy, symlinks=True)
    helper = copy / "Contents/MacOS/mcp-server"
    with helper.open("r+b") as stream:
        stream.seek(4096)
        before = stream.read(1)
        assert before
        stream.seek(4096)
        stream.write(bytes([before[0] ^ 1]))
    with pytest.raises(launchers.McpLauncherError) as caught:
        launchers.resolve_launcher(spec, platform="macos", roots=(copied_root,))
    assert caught.value.code == "mcp_launcher_untrusted"
    assert launchers.resolve_launcher(spec, platform="macos", roots=(app.parent,)) == original


# An unrelated malformed app plist is ignored during discovery, while the declared app still resolves and verifies.
def test_malformed_neighbor_does_not_break_discovery(tmp_path):
    from csk import mcp_launchers as launchers
    _, helper = bundle(tmp_path)
    neighbor = tmp_path / "Other.app/Contents"
    neighbor.mkdir(parents=True)
    (neighbor / "Info.plist").write_bytes(b'<?xml invalid?>')
    spec = parse(declaration())["app"].server.launcher
    assert launchers.resolve_launcher(spec, platform="macos", roots=(tmp_path,), verifier=lambda *a: good_verification()).path == str(helper.resolve())


# Direct and indirect helper loops refuse with the path error and preserve links; repairing the path permits verification.
@pytest.mark.parametrize("indirect", [False, True])
def test_helper_symlink_loop_refuses_before_verification(tmp_path, indirect):
    from csk import mcp_launchers as launchers
    _, helper = bundle(tmp_path)
    helper.unlink()
    target = "other" if indirect else helper.name
    helper.symlink_to(target)
    other = helper.parent / "other"
    if indirect:
        other.symlink_to(helper.name)
    spec = parse(declaration())["app"].server.launcher
    with pytest.raises(launchers.McpLauncherError) as caught:
        launchers.resolve_launcher(spec, platform="macos", roots=(tmp_path,), verifier=lambda *a: pytest.fail("loop reached verifier"))
    assert caught.value.code == "mcp_launcher_path_invalid"
    assert helper.readlink() == Path(target)
    helper.unlink()
    if indirect:
        assert other.readlink() == Path(helper.name)
        other.unlink()
    helper.write_bytes(b"repaired fixture")
    helper.chmod(0o755)
    assert launchers.resolve_launcher(spec, platform="macos", roots=(tmp_path,), verifier=lambda *a: good_verification()).path == str(helper.resolve())


# On Windows/Linux the host dispatcher uses that platform and rejects an absent helper before any macOS verifier call.
@pytest.mark.skipif(sys.platform == "darwin", reason="non-macOS host dispatch")
def test_host_dispatch_resolves_current_platform(tmp_path):
    from csk import mcp_launchers as launchers
    spec = parse(declaration({"macos": MAC, "windows": WINDOWS, "linux": LINUX}))["app"].server.launcher
    with pytest.raises(launchers.McpLauncherError) as caught:
        launchers.resolve_launcher(spec, roots=(tmp_path,), verifier=lambda *a: pytest.fail("missing helper reached verification"))
    assert caught.value.code == "mcp_launcher_path_invalid"


# Local path/content observations never enter the portable declaration digest, while portable publisher edits do change it.
def test_declaration_digest_excludes_resolved_observations():
    from csk import mcp_launchers as launchers
    original = parse(declaration())["app"]
    expected = declarations.declaration_digest(original)
    observation = launchers.ResolvedLauncher("/Applications/App.app/Contents/MacOS/mcp-server", "verified", "publisher", "a" * 64)
    observed = replace(original, server=replace(original.server, resolved_launcher=observation))
    assert declarations.declaration_digest(observed) == expected
    assert declarations.declaration_digest(replace(observed, server=replace(observed.server, resolved_launcher=replace(observation, content_sha256="b" * 64)))) == expected
    raw = declaration()
    raw["app"]["server"]["launcher"]["macos"]["team_id"] = "OTHER12345"
    assert declarations.declaration_digest(parse(raw)["app"]) != expected
