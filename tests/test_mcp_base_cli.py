"""Reviewed default-wrapper and interpreter regressions through real CLI paths."""
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from test_mcp_project_install import project_cli
from test_mcp_install_acceptance import _snapshot
from test_mcp_command_resolution import stdio, runner_guard
import errno
import select
import sys
import time

pytestmark = [pytest.mark.usefixtures("stable_env"), pytest.mark.skipif(sys.platform != "darwin", reason="macOS dispatch witnesses")]
VIM_ARGS = ["-Nu", "NONE", "-i", "NONE", "-n", "-es", "-c", "call system('n'.'px pkg')", "-c", "qa!"]


def cli(root,*args):
    return subprocess.run([sys.executable,'-m','csk',*args],cwd=root,env=os.environ.copy(),stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30)

def terminal(root):
    import pty
    master,slave=pty.openpty()
    child=subprocess.Popen([sys.executable,'-m','csk','install','app'],cwd=root,env=os.environ.copy(),stdin=slave,stdout=slave,stderr=slave)
    os.close(slave)
    output=b''; replied=False
    try:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if select.select([master],[],[],0.1)[0]:
                try: data=os.read(master,65536)
                except OSError as exc:
                    if exc.errno==errno.EIO: break
                    raise
                if not data: break
                output+=data
                if b'[y/N]' in output and not replied:
                    os.write(master,b'y\n');replied=True
            elif child.poll() is not None: break
        rc=child.wait(timeout=3)
    finally:
        if child.poll() is None: child.terminate();child.wait(timeout=5)
        os.close(master)
    return rc,replied,output.decode(errors='replace')


def executable(path, data=b"\x7fELFopaque binary fixture\n"):
    path.write_bytes(data); path.chmod(0o700); return path


def _stub(tmp_path, monkeypatch, search):
    return runner_guard(tmp_path, monkeypatch, search)


def _oracles(root, argv, marker):
    # ssh-agent would start an agent service. Its static base-command refusal
    # is exercised below without starting that service in this test lane.
    if argv[0] == "ssh-agent":
        assert Path("/usr/bin/ssh-agent").is_file()
        return

    # Positive dispatch oracles: BSD execvp (/usr/bin/env), libuv (node, when
    # present) and Python subprocess each reach only the owned recording stub.
    runners = [["/usr/bin/env", *argv], [os.sys.executable, "-I", "-c",
               "import subprocess,sys; sys.exit(subprocess.run(sys.argv[1:], stdin=subprocess.DEVNULL, capture_output=True).returncode)", *argv]]
    node = shutil.which("node")
    if node:
        runners.append([node, "-e", "const r=require('child_process').spawnSync(process.argv[1],process.argv.slice(2),{stdio:'ignore'});process.exit(r.status??99)", *argv])
    for runner in runners:
        if marker.exists(): marker.unlink()
        for leftover in ("review.lock", "review-agent.sock"):
            if (root / leftover).exists(): (root / leftover).unlink()
        done = subprocess.run(runner, cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        assert done.returncode == 0 and marker.read_text() == "pkg\n", (runner[0], done)
    marker.unlink()


def _admission(root, home, declare, command, args, marker, kind):
    declare(stdio(command, args))
    before = _snapshot(root, home)
    checked = cli(root, "check", "app")
    dry = cli(root, "install", "app", "--dry-run")
    assert _snapshot(root, home) == before and not marker.exists()
    rc, approval, transcript = terminal(root)
    assert not marker.exists(), "admission must never execute a command"
    native = json.loads((root / ".mcp.json").read_text()) if (root / ".mcp.json").exists() else None
    replay = None
    if native:
        entry = native["mcpServers"]["bad"]
        done = subprocess.run([entry["command"], *entry["args"]], cwd=root, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)
        replay = (done.returncode, marker.read_text() if marker.exists() else None)
    print(json.dumps({"kind": kind, "check_exit": checked.returncode, "check_out": (checked.stdout + checked.stderr)[-300:],
                      "dry_exit": dry.returncode, "install_exit": rc, "approval": approval,
                      "native": native, "native_replay": replay}))
    assert checked.returncode == 1 and "mcp_command_invalid" in checked.stdout + checked.stderr
    assert dry.returncode == 1 and rc == 1 and not approval and native is None
    assert _snapshot(root, home) == before


# F1: a default-install macOS program that evaluates a shell string must be
# refused as command. Static; no host alias or PATH fixture is needed.
def test_apply_shell_evaluator_real_cli(project_cli, tmp_path, monkeypatch):
    root, home, declare = project_cli
    if not Path("/usr/bin/apply").exists():
        pytest.skip("requires /usr/bin/apply")
    marker = _stub(tmp_path, monkeypatch, str(tmp_path / "none"))
    args = ["-0", "n''px pkg", "x"]
    _oracles(root, ["apply", *args], marker)
    _admission(root, home, declare, "apply", args, marker, "apply")


# F1: default-install wrappers missing from the indirection table route around
# the host check. The wrapped program is a non-table PATH alias of system Vim
# (the r5 alias shape) or the real Homebrew lua-5.5 alias when present.
@pytest.mark.parametrize("wrapper", ["lockf", "xcrun", "sandbox-exec", "ssh-agent"])
@pytest.mark.parametrize("target", ["fixture-vim-alias", "homebrew-lua-5.5"])
def test_default_wrapper_hides_alias_real_cli(project_cli, tmp_path, monkeypatch, wrapper, target):
    root, home, declare = project_cli
    if not shutil.which(wrapper, path="/usr/bin"):
        pytest.skip(f"requires /usr/bin/{wrapper}")
    a = tmp_path / "bin"; a.mkdir()
    if target == "fixture-vim-alias":
        (a / "myedit").symlink_to("/usr/bin/vim")
        inner = ["myedit", *VIM_ARGS]
    else:
        if not Path("/opt/homebrew/bin/lua-5.5").exists():
            pytest.skip("requires Homebrew lua-5.5")
        inner = ["lua-5.5", "-e", "os.execute('n'..'px pkg')"]
    marker = _stub(tmp_path, monkeypatch, str(a) + os.pathsep + "/opt/homebrew/bin")
    prefix = {"lockf": ["-k", "review.lock"], "xcrun": [], "sandbox-exec": ["-p", "(version 1)(allow default)"],
              "ssh-agent": ["-a", "review-agent.sock"]}[wrapper]
    args = [*prefix, *inner]
    _oracles(root, [wrapper, *args], marker)
    for leftover in ("review.lock", "review-agent.sock"):
        if (root / leftover).exists(): (root / leftover).unlink()
    _admission(root, home, declare, wrapper, args, marker, f"{wrapper}/{target}")


# F2: execvp/posix_spawn skip a selected regular X_OK file whose execve fails
# with ENOENT/EACCES because its interpreter cannot be executed; the later
# evaluator then runs. Admission must not accept the unexecutable first file.
@pytest.mark.parametrize("kind", ["missing-interpreter", "crlf-interpreter", "directory-interpreter", "nonexec-interpreter"])
def test_unexecutable_interpreter_hides_later_evaluator(project_cli, tmp_path, monkeypatch, kind):
    root, home, declare = project_cli
    a = tmp_path / "bin"; a.mkdir()
    b = tmp_path / "later"; b.mkdir()
    opq = tmp_path / "opq"; opq.mkdir()
    opaque = opq / "opaque"; shutil.copyfile("/usr/bin/true", opaque); opaque.chmod(0o700)
    line = {"missing-interpreter": b"#!" + str(tmp_path / "missing").encode() + b"\n",
            "crlf-interpreter": b"#!" + str(opaque).encode() + b"\r\n",
            "directory-interpreter": b"#!" + str(opq).encode() + b"\n",
            "nonexec-interpreter": b"#!" + str(opaque).encode() + b"\n"}[kind]
    if kind == "nonexec-interpreter":
        opaque.chmod(0o600)
    executable(a / "myedit", line)
    (b / "myedit").symlink_to("/usr/bin/vim")
    marker = _stub(tmp_path, monkeypatch, str(a) + os.pathsep + str(b))
    _oracles(root, ["myedit", *VIM_ARGS], marker)
    _admission(root, home, declare, "myedit", VIM_ARGS, marker, kind)


# F3: the kernel (XNU, 512-byte limit) honours a shebang line longer than the
# 256-byte read window; an unterminated first line must not be accepted.
@pytest.mark.parametrize("kind", ["padded", "long-path"])
def test_long_shebang_fails_open(project_cli, tmp_path, monkeypatch, kind):
    root, home, declare = project_cli
    a = tmp_path / "bin"; a.mkdir()
    if kind == "padded":
        executable(a / "myedit", b"#!" + b" " * 300 + b"/usr/bin/vim\n")
    else:
        deep = tmp_path.joinpath(*(["d" * 40] * 8)); deep.mkdir(parents=True)
        (deep / "ed2").symlink_to("/usr/bin/vim")
        assert len(str(deep / "ed2")) > 260
        executable(a / "myedit", b"#!" + str(deep / "ed2").encode() + b"\n")
    marker = _stub(tmp_path, monkeypatch, str(a))
    _oracles(root, ["myedit", *VIM_ARGS], marker)
    _admission(root, home, declare, "myedit", VIM_ARGS, marker, kind)


# Positive control: a first script whose interpreter really executes wins, so
# a later evaluator must not cause refusal of the selected opaque program.
def test_runnable_interpreter_first_control(project_cli, tmp_path, monkeypatch):
    root, home, declare = project_cli
    a = tmp_path / "bin"; a.mkdir(); b = tmp_path / "later"; b.mkdir()
    executable(a / "myedit", b"#!/usr/bin/true\n")
    (b / "myedit").symlink_to("/usr/bin/vim")
    marker = _stub(tmp_path, monkeypatch, str(a) + os.pathsep + str(b))
    assert subprocess.run(["/usr/bin/env", "myedit", *VIM_ARGS], cwd=root, stdin=subprocess.DEVNULL, timeout=10).returncode == 0
    assert not marker.exists()
    declare(stdio("myedit", VIM_ARGS))
    assert cli(root, "check", "app").returncode == 0
    rc, approval, transcript = terminal(root)
    assert rc == 0 and approval and not marker.exists()
