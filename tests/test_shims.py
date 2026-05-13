from __future__ import annotations

import os
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

