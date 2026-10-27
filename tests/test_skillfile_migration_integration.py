"""Real CLI witnesses for composing the two explicit legacy migrations."""

import errno
import json
import os
import subprocess
import sys

import pytest

from test_mcp_install_acceptance import _snapshot
from test_mcp_project_install import project_cli


def command(root, *args, terminal=False):
    argv = [sys.executable, "-m", "csk", *args, "--project", str(root)]
    if not terminal:
        return subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=60)
    if os.name == "nt":
        pytest.skip("real terminal consent witness requires a POSIX PTY")
    import pty

    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(argv, cwd=root, stdin=slave, stdout=slave, stderr=subprocess.PIPE)
        os.close(slave)
        slave = None
        os.write(master, b"yes\n")
        try:
            _, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        output = bytearray()
        os.set_blocking(master, False)
        while True:
            try:
                block = os.read(master, 65536)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EAGAIN):
                    break
                raise
            if not block:
                break
            output.extend(block)
        return subprocess.CompletedProcess(argv, process.returncode, output.decode(), stderr.decode())
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)


def document(selector):
    return (
        b'{\r\n "schema_version":3, "ignore_mode"\t: "exclude",\r\n'
        b' "agents":["claude_code"], "skills":[],\r\n'
        b' "mcp_servers":{"docs":{"server":{"transport":"http",'
        b'"url":"https://mcp.example.invalid/mcp","environments":'
        + selector + b'}}}\r\n}\r\n'
    )


def refusal(root, home):
    before = _snapshot(root, home)
    result = command(root, "add", "next", "--tag", "v1")
    assert result.returncode == 2, result
    assert "skillfile_deprecated_form" in result.stderr
    assert _snapshot(root, home) == before


# Either migration order removes only its own bytes, reloads, and keeps ordinary writers blocked until both forms are gone.
@pytest.mark.parametrize("first", ["ignore", "cursor"])
@pytest.mark.parametrize("mode", ["exclude", "gitignore"])
@pytest.mark.parametrize("selector,canonical,targets", [
    (b'[ "claude_code" ,\r\n "cursor" ]', b'[ "claude_code" ]', ()),
    (b'[ "\\u0063ursor" ]', b'["claude_code"]', ("--targets", "claude_code")),
])
def test_joint_migrations_real_cli(project_cli, first, mode, selector, canonical, targets):
    root, home, _ = project_cli
    path = root / "Skillfile.json"
    raw = document(selector)
    path.write_bytes(raw)
    refusal(root, home)
    for migration in (first, "cursor" if first == "ignore" else "ignore"):
        before = path.read_bytes()
        if migration == "ignore":
            result = command(root, "ignore-mode", "set", mode)
            expected = before.replace(b'"ignore_mode"\t: "exclude",', b"")
        else:
            result = command(root, "mcp", "migrate", *targets, terminal=True)
            expected = before.replace(selector, canonical)
        assert result.returncode == 0, result
        assert result.stderr.count("not portable") == (2 if migration == first else 1)
        assert path.read_bytes() == expected
        loaded = json.loads(expected)
        assert ("ignore_mode" in loaded) == (migration == "cursor" and first == "cursor")
        assert ("cursor" in loaded["mcp_servers"]["docs"]["server"]["environments"]) == (migration == "ignore" and first == "ignore")
        shown = command(root, "ignore-mode", "show", "--json")
        assert shown.returncode == 0, shown
        assert json.loads(shown.stdout)["mode"] == ("exclude" if "ignore_mode" in loaded else mode)
        if migration == first:
            refusal(root, home)
    checked = subprocess.run([sys.executable, "-m", "csk", "check", "app"], cwd=root, capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked
    assert "schema_version 3 valid" in checked.stdout
    result = command(root, "add", "next", "--tag", "v1")
    assert result.returncode == 0, result
    assert json.loads(path.read_bytes())["skills"][0]["name"] == "next"


# Both real read-only command routes warn once per form on each invocation; clean neighbours produce valid stdout without warnings.
@pytest.mark.parametrize("args", [("ignore-mode", "show", "--json"), ("mcp", "local-target", "list")])
def test_shared_warnings_real_cli(project_cli, args):
    root, _, _ = project_cli
    path = root / "Skillfile.json"
    path.write_bytes(document(b'["claude_code","cursor"]'))
    for _ in range(2):
        result = command(root, *args)
        assert result.returncode == 0, result
        assert result.stderr.count("not portable") == 2
        assert result.stderr.count("run `csk ignore-mode set") == 1
        assert result.stderr.count("run `csk mcp migrate") == 1
        assert "warning:" not in result.stdout
        if args[0] == "ignore-mode":
            assert json.loads(result.stdout)["deprecated_ignore_mode"] == "exclude"
    path.write_bytes(document(b'["claude_code"]').replace(b'"ignore_mode"\t: "exclude",', b""))
    result = command(root, *args)
    assert result.returncode == 0, result
    assert "not portable" not in result.stderr
