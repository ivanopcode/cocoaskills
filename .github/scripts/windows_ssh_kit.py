"""Disposable native Windows SSH CI kit; never use with personal keys or data."""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

NATIVE_CASES = {
    *(f"test_windows_real_git_ssh_clean_environment_matrix[{key}]"
      for key in ("ed25519", "rsa", "ecdsa", "encrypted", "agent")),
    *(f"test_windows_native_ssh_starts_with_private_program_data[{value}]"
      for value in ("absent", "hostile")),
}


def verify_results(results: Path) -> None:
    """Require the real five-route matrix and both startup probes, never skips."""
    cases = [case for case in ET.parse(results).iter("testcase")
             if case.get("classname", "").rsplit(".", 1)[-1] == "test_git_admission_ssh"
             and case.get("name", "").startswith(("test_windows_real_git_ssh_", "test_windows_native_ssh_"))]
    names = [case.get("name") for case in cases]
    bad = [case.get("name") for case in cases if any(case.find(tag) is not None
                                                     for tag in ("skipped", "failure", "error"))]
    if set(names) != NATIVE_CASES or len(names) != len(NATIVE_CASES) or bad:
        raise ValueError("native Windows SSH cases must run exactly once without skips or failures: "
                         f"missing={sorted(NATIVE_CASES - set(names))}, bad={bad}, count={len(names)}")
    print(f"verified {len(names)} native Windows SSH cases; zero skips/failures", flush=True)


def checked(args: list[str]) -> str:
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{Path(args[0]).name} timed out; stderr={error.stderr!r}") from error
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} exit {result.returncode}: {result.stderr}")
    return result.stdout.strip()


def ps(script: str) -> str:
    executable = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';" + script)
                              .encode("utf-16le")).decode()
    return checked([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])


def quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def save(root: Path, state: dict) -> None:
    (root / "state.json").write_text(json.dumps(state), encoding="utf-8")


def server_pids(root: Path) -> list[int]:
    config = (root / "sshd_config").as_posix()
    result = ps("@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'sshd.exe' -and "
                f"$_.CommandLine.Contains({quote(config)}) }} | ForEach-Object {{ $_.ProcessId }}) "
                "| ConvertTo-Json -Compress")
    values = json.loads(result or "[]")
    return values if isinstance(values, list) else [values]


def setup(root: Path, port: int) -> None:
    if os.name != "nt":
        raise RuntimeError("this disposable kit requires elevated native Windows")
    ssh = Path(os.environ["SYSTEMROOT"]) / "System32/OpenSSH"
    for binary in ("ssh.exe", "sshd.exe", "ssh-keygen.exe", "ssh-add.exe"):
        if not (ssh / binary).is_file():
            raise RuntimeError(f"native OpenSSH binary missing: {binary}")
    git = shutil.which("git.exe")
    if git is None:
        raise RuntimeError("Git for Windows is required")
    user = getpass.getuser()
    if not user.isascii() or not user.isalnum():
        raise RuntimeError("fixture requires a local alphanumeric test account")
    # Refuse a port already in use; never stop somebody else's listener.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    root.mkdir(mode=0o700, parents=True)  # Existing roots are never overwritten.
    service = json.loads(ps("Get-CimInstance Win32_Service -Filter \"Name='ssh-agent'\" "
                            "| Select-Object State,StartMode | ConvertTo-Json -Compress"))
    state = {"kind": "csk-windows-ssh-kit-v1", "root": str(root), "git": git, "ssh": str(ssh),
             "task": "csk-ssh-" + uuid.uuid4().hex, "service": service, "agent_add_attempted": False}
    save(root, state)  # Cleanup can recover after any subsequent setup failure.
    (root / "tmp").mkdir(mode=0o700)
    keygen = str(ssh / "ssh-keygen.exe")
    for key in ("ed25519", "rsa", "ecdsa", "encrypted", "host"):
        kind = key if key in ("rsa", "ecdsa") else "ed25519"
        # Public fixture phrase protects only a newly generated disposable key.
        phrase = "csk-generated-test-passphrase" if key == "encrypted" else ""
        checked([keygen, "-q", "-t", kind, "-N", phrase, "-C", "csk-disposable-fixture", "-f", str(root / key)])
    (root / "authorized_keys").write_text("".join((root / (key + ".pub")).read_text()
                                               for key in ("ed25519", "rsa", "ecdsa", "encrypted")), encoding="ascii")
    (root / "known_hosts").write_text(f"[127.0.0.1]:{port} " + (root / "host.pub").read_text(), encoding="ascii")
    (root / "endpoint.json").write_text(json.dumps({"port": port, "user": user}), encoding="utf-8")
    shutil.copyfile(__file__, root / "server.py")
    command = subprocess.list2cmdline([sys.executable, str(root / "server.py"), "serve", "--root", str(root)])
    (root / "sshd_config").write_text(
        f'Port {port}\nListenAddress 127.0.0.1\nHostKey "{(root / "host").as_posix()}"\n'
        f'AuthorizedKeysFile "{(root / "authorized_keys").as_posix()}"\nAllowUsers {user.lower()}\n'
        "StrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "AllowTcpForwarding no\nPermitTTY no\nLogLevel VERBOSE\n" + f"ForceCommand {command}\n", encoding="utf-8")
    ps(f"$key={quote(root / 'host')};"
       "& icacls.exe $key /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null;"
       "if($LASTEXITCODE){throw 'host key ACL failed'};"
       "& icacls.exe $key /setowner '*S-1-5-18' | Out-Null;"
       "if($LASTEXITCODE){throw 'host key owner failed'};"
       "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
       "& icacls.exe $key /remove:g \"*$sid\" | Out-Null;"
       "if($LASTEXITCODE){throw 'host key user ACE removal failed'};"
       "Set-Service ssh-agent -StartupType Manual;Start-Service ssh-agent")
    for _ in range(30):
        listing = subprocess.run([str(ssh / "ssh-add.exe"), "-l"], capture_output=True)
        if listing.returncode == 1:
            break
        if listing.returncode == 0:
            raise RuntimeError("kit refuses an agent containing existing identities")
        time.sleep(0.1)
    else:
        raise RuntimeError("native service agent did not become available")
    # Load the same identity via a briefly unlocked generated copy. This avoids
    # a CI console/askpass dependency; it does not test interactive ssh-add.
    from cryptography.hazmat.primitives import serialization

    unlocked = root / "agent-load-key"
    try:
        generated_key = serialization.load_ssh_private_key(
            (root / "encrypted").read_bytes(), password=b"csk-generated-test-passphrase")
        unlocked.write_bytes(generated_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
        # Python-created files inherit directory ACEs that native OpenSSH can
        # reject (including OWNER RIGHTS); restrict this generated copy only.
        ps("$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
           f"& icacls.exe {quote(unlocked)} /inheritance:r /grant:r \"*$sid`:F\" | Out-Null;"
           "if($LASTEXITCODE){throw 'generated agent key ACL failed'}")
        state["agent_add_attempted"] = True
        save(root, state)
        checked([str(ssh / "ssh-add.exe"), str(unlocked)])
    finally:
        unlocked.unlink(missing_ok=True)
    checked([str(ssh / "sshd.exe"), "-t", "-f", str(root / "sshd_config")])
    arguments = subprocess.list2cmdline(["-D", "-f", (root / "sshd_config").as_posix(), "-E", (root / "sshd.log").as_posix()])
    ps(f"$action=New-ScheduledTaskAction -Execute {quote(ssh / 'sshd.exe')} -Argument {quote(arguments)};"
       f"Register-ScheduledTask -TaskName {quote(state['task'])} -Action $action -User SYSTEM "
       "-RunLevel Highest | Out-Null;" + f"Start-ScheduledTask -TaskName {quote(state['task'])}")
    for _ in range(50):
        state["pids"] = server_pids(root)
        save(root, state)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2) as connection:
                if connection.recv(256).startswith(b"SSH-"):
                    break
        except OSError:
            pass
        time.sleep(0.2)
    else:
        raise RuntimeError("loopback sshd did not become available; inspect sshd.log")
    (root / "versions.log").write_text(checked([git, "--version"]) + "\n" +
        subprocess.run([str(ssh / "ssh.exe"), "-V"], capture_output=True, text=True).stderr, encoding="utf-8")
    print("disposable loopback sshd and generated agent identity ready", flush=True)


def run(root: Path, results: Path) -> int:
    results.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CSK_TEST_WINDOWS_SSH_ROOT=str(root), TEMP=str(root / "tmp"), TMP=str(root / "tmp"))
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             "tests/test_git_admission_ssh.py", "--basetemp=" + str(root / "pytest"),
                             "--junitxml=" + str(results)], env=env)
    print(f"pytest exit={result.returncode}", flush=True)
    verify_results(results)
    return result.returncode


def cleanup(root: Path, evidence: Path) -> None:
    if not (root / "state.json").is_file():
        if root.exists():
            raise RuntimeError("refusing to remove a directory without kit ownership state")
        return
    state = json.loads((root / "state.json").read_text())
    if state.get("kind") != "csk-windows-ssh-kit-v1" or state.get("root") != str(root):
        raise RuntimeError("refusing foreign kit state")
    # Record exact owned server PIDs before terminating any process; never a
    # pattern kill. The config path distinguishes the normal system sshd.
    state["pids"] = server_pids(root)
    save(root, state)
    for pid in state["pids"]:
        checked(["taskkill.exe", "/PID", str(pid), "/F"])
    task = quote(state["task"])
    ps(f"if(Get-ScheduledTask -TaskName {task} -ErrorAction SilentlyContinue) "
       f"{{Unregister-ScheduledTask -TaskName {task} -Confirm:$false}}")
    if state["agent_add_attempted"]:
        add = str(Path(state["ssh"]) / "ssh-add.exe")
        removed = subprocess.run([add, "-d", str(root / "encrypted.pub")], capture_output=True)
        remaining = subprocess.run([add, "-l"], capture_output=True)
        if removed.returncode not in (0, 1) or remaining.returncode != 1:
            raise RuntimeError("generated agent identity cleanup did not leave the initially empty agent empty")
        print("agent identity readback exit=1 (empty)", flush=True)
    service = state["service"]
    if service["State"] != "Running":
        ps("Stop-Service ssh-agent")
    mode = {"Auto": "Automatic", "Manual": "Manual", "Disabled": "Disabled"}[service["StartMode"]]
    ps(f"Set-Service ssh-agent -StartupType {mode}")
    evidence.mkdir(parents=True, exist_ok=True)
    for name in ("sshd.log", "versions.log"):
        if (root / name).is_file():
            shutil.copyfile(root / name, evidence / name)
    def writable(function, path, error):
        if isinstance(error, FileNotFoundError):
            return
        os.chmod(path, stat.S_IWRITE)
        function(path)
    shutil.rmtree(root, onexc=writable)
    assert not root.exists()
    print("kit removed; generated agent identity removed; service state restored", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "run", "cleanup", "serve", "verify"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=22224)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        if args.results is None:
            parser.error("verify requires --results")
        verify_results(args.results)
        return 0
    if args.root is None:
        parser.error("this command requires --root")
    root = args.root.resolve()
    if args.command == "setup":
        setup(root, args.port)
    elif args.command == "run":
        if args.results is None or args.results.resolve().is_relative_to(root):
            parser.error("run requires --results outside the kit root")
        return run(root, args.results.resolve())
    elif args.command == "cleanup":
        if args.evidence is None or args.evidence.resolve().is_relative_to(root):
            parser.error("cleanup requires --evidence outside the kit root")
        cleanup(root, args.evidence.resolve())
    else:
        state = json.loads((root / "state.json").read_text())
        repository = (root / "repository").read_text(encoding="utf-8").strip()
        return subprocess.call([state["git"], "upload-pack", repository])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
