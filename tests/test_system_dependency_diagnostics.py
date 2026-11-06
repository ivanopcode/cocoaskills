"""Resolution evidence distinguishes repairs without relaxing tool admission."""
import os
from pathlib import Path

import pytest

from csk import installer
from csk.tool_paths import resolve_tool
from test_launcher_tool_paths import _plan, _tool, _link, _launcher_text, _split_launcher


@pytest.fixture(autouse=True)
def isolated_resolution(monkeypatch, tmp_path):
    resolve_tool.cache_clear()
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "manager/config.json"))
    yield
    resolve_tool.cache_clear()


# Each refusal precedes publication and names the observed cause only; following
# its placement, permission or declaration remedy admits the same tool bytes.
@pytest.mark.parametrize("declaration", ["explicit", "legacy"])
@pytest.mark.parametrize("case", [
    "absent", "absent-with-shim-directory", "non-executable", "relative",
    "excluded", "absolute-excluded", "alias-excluded", "absolute-absent",
    "absolute-non-executable",
])
def test_dependency_remedies(tmp_path, monkeypatch, declaration, case):
    if "non-executable" in case and os.name == "nt":
        pytest.skip("POSIX execute-bit remedy")
    clean = tmp_path / "external tools"
    tool = _tool(clean)
    original = tool.read_bytes()
    mixed = tmp_path / "mixed-bin"
    mixed.mkdir()
    ledger = mixed / ".csk-managed.json"
    ledger.write_text("{}")
    command = "external-helper"
    path = str(clean)
    if "absent" in case:
        tool.unlink()
        if case == "absent-with-shim-directory":
            path = str(mixed) + os.pathsep + path
    if "non-executable" in case:
        tool.chmod(0o644)
    if "excluded" in case:
        tool = tool.rename(mixed / tool.name)
        path = str(mixed)
        if case == "alias-excluded":
            _link(clean / tool.name, tool)
            path = str(clean)
    if case.startswith("absolute-"):
        command = str(tool)
    elif case == "relative":
        command = "subdir/external-helper"
    monkeypatch.setenv("PATH", path)
    plan = _plan(tmp_path / "initial", declaration, command)
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    with pytest.raises(installer.InstallError) as error:
        installer.install_runtime_commands(home, bin_dir, plan)
    diagnostic = str(error.value)
    assert "Cannot resolve system command" in diagnostic
    assert "remediation:" in diagnostic
    assert not bin_dir.exists() and not (home / "runtime").exists()
    assert ledger.read_text() == "{}"

    if "excluded" in case:
        assert "candidate crosses an excluded csk shim tree" in diagnostic
        assert ".csk-managed.json" in diagnostic
        assert "symlink chain" in diagnostic
        if case == "absolute-excluded":
            assert "change the command declaration" in diagnostic
            assert "PATH alone" in diagnostic
        # Execute the printed placement remedy. An absolute declaration must
        # also be updated; PATH alone is intentionally insufficient.
        if case == "alias-excluded":
            (clean / tool.name).unlink()
        tool = tool.rename(clean / tool.name)
        monkeypatch.setenv("PATH", str(clean))
        if case == "absolute-excluded":
            resolve_tool.cache_clear()
            with pytest.raises(installer.InstallError):
                installer.install_runtime_commands(home, bin_dir, plan)
            plan = _plan(tmp_path / "repaired", declaration, str(tool))
    else:
        assert "excluded" not in diagnostic
        assert "shim" not in diagnostic and ".csk-managed.json" not in diagnostic
        if "absent" in case:
            assert "not found" in diagnostic
            if case.startswith("absolute-"):
                assert "declared absolute path" in diagnostic
            tool = _tool(clean)
        elif "non-executable" in case:
            assert "not executable" in diagnostic
            tool.chmod(0o755)
        else:
            assert "invalid command declaration" in diagnostic
            assert "bare command name or an absolute path" in diagnostic
            plan = _plan(tmp_path / "repaired", declaration, "external-helper")

    assert tool.read_bytes() == original
    resolve_tool.cache_clear()
    assert installer.install_runtime_commands(home, bin_dir, plan) == {"tool"}
    _, suffix = _split_launcher(_launcher_text(bin_dir))
    assert str(clean.resolve()) in suffix


# A valid bare name is admitted directly, not only after a rejection/repair.
@pytest.mark.parametrize("declaration", ["explicit", "legacy"])
def test_bare_dependency_positive_control(tmp_path, monkeypatch, declaration):
    tool = _tool(tmp_path / "external")
    monkeypatch.setenv("PATH", str(tool.parent))
    plan = _plan(tmp_path, declaration)
    assert installer.install_runtime_commands(tmp_path / "home", tmp_path / "bin", plan) == {"tool"}


# Diagnostic lookups report observed candidates, not an empty excluded PATH
# directory. Manager Git/SSH keep their original failures and cached success.
@pytest.mark.parametrize("name", ["git", "ssh-add"])
@pytest.mark.parametrize("present", [False, True])
def test_manager_contract_and_exclusion_evidence(tmp_path, monkeypatch, name, present):
    from csk.tool_paths import ToolResolutionError

    mixed = tmp_path / ".agents/bin"
    mixed.mkdir(parents=True)
    candidate = mixed / (name + ".exe" if os.name == "nt" else name)
    if present:
        candidate.write_bytes(b"fixture")
        candidate.chmod(0o755)
    monkeypatch.setenv("PATH", str(mixed))
    message = f"{name} executable not found outside project and csk shims; install {name} and ensure it is on PATH"
    with pytest.raises(FileNotFoundError) as old:
        resolve_tool(name)
    assert type(old.value) is FileNotFoundError
    assert str(old.value) == message
    with pytest.raises(ToolResolutionError) as detailed:
        resolve_tool(name, diagnostics=True)
    assert str(detailed.value) == message
    assert any(item.reason == "excluded" for item in detailed.value.rejections) is present
    clean = tmp_path / "external"
    clean.mkdir()
    real = clean / candidate.name
    real.write_bytes(b"fixture")
    real.chmod(0o755)
    monkeypatch.setenv("PATH", str(mixed) + os.pathsep + str(clean))
    assert resolve_tool(name) == str(real.resolve())
    assert resolve_tool(name, diagnostics=True) == str(real.resolve())
    monkeypatch.setenv("PATH", str(tmp_path / "missing"))
    assert resolve_tool(name) == str(real.resolve())


# Bad declaration spelling is escaped even in the new form-specific guidance.
def test_invalid_declaration_is_terminal_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    plan = _plan(tmp_path, "explicit", "subdir/\x1b[31mhelper\n\u202e")
    with pytest.raises(installer.InstallError) as error:
        installer.install_runtime_commands(tmp_path / "home", tmp_path / "bin", plan)
    diagnostic = str(error.value)
    assert "invalid command declaration" in diagnostic
    assert not any(character in diagnostic for character in ("\x1b", "\n", "\u202e"))


# A directory, a looping link and a failed read are unusable, not evidence of
# shim exclusion or mere absence. Replacing the candidate repairs each case.
@pytest.mark.parametrize("kind", ["directory", "loop", "read-error"])
def test_unusable_dependency_is_not_misdiagnosed(tmp_path, monkeypatch, kind):
    tool = _tool(tmp_path / "external")
    monkeypatch.setenv("PATH", str(tool.parent))
    plan = _plan(tmp_path, "explicit")
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    if kind != "read-error":
        tool.unlink()
        if kind == "directory":
            tool.mkdir()
        else:
            _link(tool, tool)
    with monkeypatch.context() as patch:
        if kind == "read-error":
            native_lstat = Path.lstat

            def denied(path, *args, **kwargs):
                if path == tool:
                    raise PermissionError("fixture read denied")
                return native_lstat(path, *args, **kwargs)

            patch.setattr(Path, "lstat", denied)
        with pytest.raises(installer.InstallError) as error:
            installer.install_runtime_commands(home, bin_dir, plan)
    diagnostic = str(error.value)
    assert "no usable regular executable could be resolved" in diagnostic
    assert "excluded" not in diagnostic and "not found" not in diagnostic
    assert not bin_dir.exists() and not (home / "runtime").exists()
    if kind == "directory":
        tool.rmdir()
    else:
        tool.unlink()
    _tool(tool.parent)
    resolve_tool.cache_clear()
    assert installer.install_runtime_commands(home, bin_dir, plan) == {"tool"}
