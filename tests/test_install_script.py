from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "docs" / "install.sh"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="install.sh requires POSIX sh")

SHIM = """#!{python}
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
profile = json.loads(os.environ["INSTALL_TEST_PROFILE"])
with open(os.environ["INSTALL_TEST_TRACE"], "a") as stream:
    stream.write(json.dumps([name, args]) + "\\n")

def install(client):
    with open(os.environ["INSTALL_TEST_INSTALLS"], "a") as stream:
        stream.write(json.dumps(dict(client=client, args=args, system_certs=os.environ.get("UV_SYSTEM_CERTS"))) + "\\n")
    sys.exit(profile.get("install_exit", 0))

if name == "uv":
    if args == ["--version"]:
        print("uv " + profile["uv"])
        sys.exit(profile.get("version_exit", {{}}).get("uv", 0))
    elif args[:2] == ["tool", "install"]:
        install("uv")
    else:
        sys.exit(9)
elif name == "pipx":
    if args == ["--version"]:
        print(profile["pipx"])
    elif args == ["environment", "--value", "PIPX_SHARED_LIBS"]:
        if profile.get("shared_location", "valid") == "error":
            sys.exit(2)
        location = profile.get("shared_location", "valid")
        print("" if location == "empty" else profile["shared_path"] + ("/missing" if location == "missing" else ""))
        if profile.get("shared_location") == "error-output":
            sys.exit(2)
    elif args[:1] == ["install"]:
        install("pipx")
    else:
        sys.exit(9)
elif name in ("python3", "python"):
    if args[:2] == ["-m", "pip"]:
        version = profile["shared" if name == "python" else "pip"]
        if args[2:] == ["--version"]:
            if version is None:
                sys.exit(2)
            print("pip " + version + " from /test/site-packages/pip (python 3.12)")
            sys.exit(profile.get("version_exit", {{}}).get("shared" if name == "python" else "pip", 0))
        elif args[2:3] == ["install"]:
            install("pip")
        else:
            sys.exit(9)
    elif args[:2] == ["-m", "ensurepip"]:
        install("ensurepip")
    elif args == ["-m", "uv", "tool", "dir", "--bin"]:
        print(Path(sys.argv[0]).parent)
    else:
        os.execv(os.environ["INSTALL_TEST_PYTHON"], [os.environ["INSTALL_TEST_PYTHON"], *args])
elif name == "pip":
    install("standalone-pip")
elif name == "brew":
    if args == ["--prefix"]:
        print(profile["brew_prefix"])
    elif args != ["install", "curl", "ca-certificates"]:
        sys.exit(9)
elif name == "curl":
    if profile.get("workflow") == "macOS" and "/opt/curl/bin/" not in sys.argv[0]:
        sys.exit(8)
    print(Path(os.environ["INSTALL_TEST_SCRIPT"]).read_text(), end="")
else:
    sys.exit(9)
"""


def _run(
    tmp_path: Path,
    *,
    uv: str | None = "0.13.4",
    pipx: str | None = "1.8.0",
    shared: str | None = "26.2.2",
    pip: str | None = "26.2.2",
    shared_location: str = "valid",
    pin: str | None = None,
    install_exit: int = 0,
    version_exit: dict[str, int] | None = None,
    workflow: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[dict], list[list]]:
    binary = tmp_path / "bin"
    binary.mkdir()
    shared_path = tmp_path / "shared libs"
    shared_python = shared_path / "bin" / "python"
    shared_python.parent.mkdir(parents=True)
    shims = [binary / "python3", binary / "pip", shared_python]
    if uv is not None:
        shims.append(binary / "uv")
    if pipx is not None:
        shims.append(binary / "pipx")
    if workflow:
        shims.extend([binary / "python", binary / "brew", binary / "curl"])
        brew_curl = tmp_path / "brew" / "opt" / "curl" / "bin" / "curl"
        brew_curl.parent.mkdir(parents=True)
        shims.append(brew_curl)
        shell = shutil.which("sh")
        assert shell is not None
        (binary / "sh").symlink_to(shell)
    for path in shims:
        path.write_text(SHIM.format(python=sys.executable), encoding="utf-8")
        path.chmod(0o755)
    installs = tmp_path / "installs.jsonl"
    trace = tmp_path / "trace.jsonl"
    profile = {
        "uv": uv,
        "pipx": pipx,
        "shared": shared,
        "pip": pip,
        "shared_path": str(shared_path),
        "shared_location": shared_location,
        "install_exit": install_exit,
        "version_exit": version_exit or {},
        "workflow": workflow,
        "brew_prefix": str(tmp_path / "brew"),
    }
    environment = {
        "PATH": str(binary),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "INSTALL_TEST_PYTHON": sys.executable,
        "INSTALL_TEST_PROFILE": json.dumps(profile),
        "INSTALL_TEST_INSTALLS": str(installs),
        "INSTALL_TEST_TRACE": str(trace),
        "INSTALL_TEST_SCRIPT": str(SCRIPT),
    }
    if pin is not None:
        environment["CSK_VERSION"] = pin
    shell = shutil.which("sh")
    assert shell is not None
    command = [shell, str(SCRIPT)]
    if workflow:
        import yaml

        data = yaml.safe_load(
            (
                SCRIPT.parents[1] / ".github" / "workflows" / "distribution-smoke.yml"
            ).read_text()
        )
        step = next(
            step
            for step in data["jobs"]["smoke"]["steps"]
            if step["name"] == "Install with install.sh"
        )
        command = [shell, "-eu", "-c", step["run"]]
        environment.update(
            RUNNER_OS=workflow, GITHUB_PATH=str(tmp_path / "github-path")
        )
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return (
        result,
        [json.loads(line) for line in installs.read_text().splitlines()]
        if installs.exists()
        else [],
        [json.loads(line) for line in trace.read_text().splitlines()],
    )


@pytest.mark.parametrize("version", ("0.13.4", "0.13.5", "0.14.0", "0.100.0", "1.0.0"))
def test_acceptable_uv_has_priority_and_system_certificates(
    tmp_path: Path, version: str
):
    # Accepted uv wins over both alternatives and installs with platform certificates enabled.
    result, installs, trace = _run(tmp_path, uv=version)
    assert result.returncode == 0, result.stderr
    assert installs == [
        {
            "client": "uv",
            "args": ["tool", "install", "--force", "cocoaskills"],
            "system_certs": "1",
        }
    ]
    assert not any(name == "pipx" for name, _ in trace)


@pytest.mark.parametrize(
    "version", ("0.13.3", "0.11.3", "0.9.99", "0.13.4rc1", "unknown")
)
def test_old_or_unestablished_uv_falls_back_to_shared_pip(tmp_path: Path, version: str):
    # Unsafe or unknown uv cannot install; verified pipx shared pip is the next choice.
    result, installs, trace = _run(tmp_path, uv=version)
    assert result.returncode == 0, result.stderr
    assert [item["client"] for item in installs] == ["pipx"]
    assert ["pipx", ["environment", "--value", "PIPX_SHARED_LIBS"]] in trace
    assert ["python", ["-m", "pip", "--version"]] in trace


@pytest.mark.parametrize("shared", ("26.2.1", "26.2.2rc1", "garbage", None))
def test_pipx_without_acceptable_shared_pip_uses_direct_pip(
    tmp_path: Path, shared: str | None
):
    # Rejected shared pip never installs through pipx; a verified direct pip remains usable.
    result, installs, _ = _run(tmp_path, uv="0.13.3", shared=shared)
    assert result.returncode == 0, result.stderr
    assert [item["client"] for item in installs] == ["pip"]


@pytest.mark.parametrize("location", ("empty", "error", "error-output", "missing"))
def test_unknown_pipx_shared_location_cannot_install(tmp_path: Path, location: str):
    # Failed or empty pipx environment output cannot authorize its installation backend.
    result, installs, _ = _run(tmp_path, uv=None, shared_location=location)
    assert result.returncode == 0, result.stderr
    assert [item["client"] for item in installs] == ["pip"]


@pytest.mark.parametrize("version", ("26.2.2", "26.2.3", "26.2.10", "26.3", "27.0"))
def test_direct_pip_floor_and_newer_releases(tmp_path: Path, version: str):
    # Stable pip at or above its floor is usable when no preferred client is present.
    result, installs, _ = _run(tmp_path, uv=None, pipx=None, pip=version)
    assert result.returncode == 0, result.stderr
    assert [item["client"] for item in installs] == ["pip"]


def test_all_old_clients_refuse_without_any_install(tmp_path: Path):
    # All-old refusal lists discovered versions, floors and upgrade commands without installs.
    result, installs, _ = _run(tmp_path, uv="0.13.3", shared="26.2.1", pip="26.2.1")
    assert result.returncode != 0
    assert installs == []
    for value in (
        "uv",
        "0.13.3",
        "0.13.4",
        "uv self update",
        "pipx",
        "1.8.0",
        "26.2.1",
        "26.2.2",
        "pipx upgrade-shared",
        "python3 -m pip install --upgrade pip",
    ):
        assert value in result.stderr
    assert result.stderr.count("cocoaskills install:") == 1


@pytest.mark.parametrize("shared", ("26.2.1", "garbage", None))
def test_unacceptable_shared_pip_cannot_install_without_safe_fallback(
    tmp_path: Path, shared: str | None
):
    # Old or unknown shared pip plus an old direct pip authorizes no installation at all.
    result, installs, _ = _run(tmp_path, uv=None, shared=shared, pip="26.2.1")
    assert result.returncode != 0
    assert installs == []
    assert "pipx" in result.stderr
    assert "pipx upgrade-shared" in result.stderr
    assert "26.2.2" in result.stderr


@pytest.mark.parametrize("version", ("26.2.1", "26.2.2rc1", "garbage", None))
def test_old_unknown_or_missing_pip_cannot_install_or_bootstrap(
    tmp_path: Path, version: str | None
):
    # Unacceptable or missing direct pip fails without installing or bootstrapping an older pip.
    result, installs, _ = _run(tmp_path, uv=None, pipx=None, pip=version)
    assert result.returncode != 0
    assert installs == []
    assert "26.2.2" in result.stderr
    assert "python3 -m pip install --upgrade pip" in result.stderr


@pytest.mark.parametrize("client", ("uv", "pipx", "pip"))
def test_version_pin_survives_each_install_path(tmp_path: Path, client: str):
    # CSK_VERSION selects the exact package spec on each accepted installation path.
    result, installs, _ = _run(
        tmp_path,
        uv="0.13.4" if client == "uv" else None,
        pipx="1.8.0" if client == "pipx" else None,
        pin="0.20.0",
    )
    assert result.returncode == 0, result.stderr
    assert len(installs) == 1
    assert installs[0]["client"] == client
    assert installs[0]["args"][-1] == "cocoaskills==0.20.0"


def test_failed_accepted_install_does_not_try_another_client(tmp_path: Path):
    # A chosen client's install failure propagates instead of silently performing a second install.
    result, installs, _ = _run(tmp_path, install_exit=7)
    assert result.returncode == 7
    assert [item["client"] for item in installs] == ["uv"]


@pytest.mark.parametrize("probe", ("uv", "shared", "pip"))
def test_failed_version_probe_cannot_authorize_install(tmp_path: Path, probe: str):
    # A failed probe's plausible version text is not evidence that its installer is acceptable.
    result, installs, _ = _run(
        tmp_path,
        uv="0.13.4" if probe == "uv" else None,
        pipx="1.8.0" if probe in {"uv", "shared"} else None,
        version_exit={probe: 2},
    )
    if probe == "pip":
        assert result.returncode != 0
        assert installs == []
    else:
        assert result.returncode == 0, result.stderr
        assert [item["client"] for item in installs] == [
            "pipx" if probe == "uv" else "pip"
        ]


@pytest.mark.parametrize("runner", ("Linux", "macOS"))
def test_distribution_smoke_bootstraps_floors_and_runs_installer(
    tmp_path: Path, runner: str
):
    # The actual CI shell block requests both floors and uses updated curl on macOS before selecting uv.
    result, installs, trace = _run(tmp_path, workflow=runner, pin="0.20.0")
    assert result.returncode == 0, result.stderr
    assert len(installs) == 2
    assert installs[0]["args"] == [
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip>=26.2.2",
        "uv>=0.13.4",
    ]
    assert installs[1] == {
        "client": "uv",
        "args": ["tool", "install", "--force", "cocoaskills==0.20.0"],
        "system_certs": "1",
    }
    assert ["curl", ["-fsSL", "https://cocoaskills.org/install.sh"]] in trace
