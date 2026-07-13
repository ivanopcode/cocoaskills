from __future__ import annotations

import os
import subprocess
import sys

import pytest

from csk import shims
from csk.skillspec import CommandSpec


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shim layout uses symlinks")
def test_unix_shim_is_symlink_and_runtime_is_executable(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    script = snapshot / "scripts" / "tool"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    command = CommandSpec(name="tool", type="script", unix_path="scripts/tool")
    runtime = shims.install_runtime_command(
        csk_home=tmp_path / "home",
        skill_name="skill",
        commit="abc",
        snapshot=snapshot,
        command=command,
        platform_name="unix",
    )
    shim = shims.write_project_shim(tmp_path / "project", "tool", runtime, platform_name="unix")
    assert shim.is_symlink()
    assert os.access(runtime, os.X_OK)


def test_windows_shim_is_cmd_wrapper(tmp_path):
    runtime = tmp_path / "home" / "runtime" / "skill" / "abc" / "bin" / "tool.cmd"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("@echo off\r\n", encoding="utf-8")
    shim = shims.write_project_shim(tmp_path / "project", "tool", runtime, platform_name="windows")
    assert shim.name == "tool.cmd"
    assert str(runtime) in shim.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="Executes a POSIX shim")
def test_unix_wrapper_prepends_runtime_path_entries(tmp_path):
    helper_bin = tmp_path / "helper bin"
    helper_bin.mkdir()
    helper = helper_bin / "helper"
    helper.write_text("#!/bin/sh\necho resolved\n", encoding="utf-8")
    helper.chmod(0o755)
    runtime = tmp_path / "runtime" / "tool"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\nhelper\n", encoding="utf-8")
    runtime.chmod(0o755)

    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool",
        runtime,
        platform_name="unix",
        path_entries=(helper_bin,),
    )
    proc = subprocess.run(
        [str(shim)],
        check=True,
        text=True,
        capture_output=True,
        env={"PATH": os.defpath},
    )

    assert not shim.is_symlink()
    assert proc.stdout.strip() == "resolved"


def test_windows_wrapper_prepends_runtime_path_entries(tmp_path):
    runtime = tmp_path / "home" / "runtime" / "skill" / "abc" / "bin" / "tool.cmd"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("@echo off\r\n", encoding="utf-8")
    helper_bin = tmp_path / "helper bin"

    shim = shims.write_project_shim(
        tmp_path / "project",
        "tool",
        runtime,
        platform_name="windows",
        path_entries=(helper_bin,),
    )
    content = shim.read_text(encoding="utf-8")

    assert "setlocal" in content
    assert f'set "PATH={helper_bin};%PATH%"' in content
    assert f'call "{runtime}" %*' in content
