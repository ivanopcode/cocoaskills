"""Execute the documented POSIX recipe using a local hash-checked wheel."""
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import zipfile

import pytest


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Documented POSIX shell recipe")


def _recipe():
    guide = Path(__file__).resolve().parents[1] / "docs/skill-authoring.md"
    section = guide.read_text().split("### Python virtual environments outside the runtime", 1)[1].split("## 2.", 1)[0]
    setup, invoke = re.findall(r"```sh\n(.*?)```", section, flags=re.S)
    return setup, invoke


def _environment(tmp_path, monkeypatch, xdg):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.defpath)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    cache = tmp_path / "cache with spaces" if xdg else tmp_path / "home/.cache"
    if xdg:
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    else:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    return cache / "example-skill/venv"


def _run(script, runtime):
    return subprocess.run(
        ["/bin/sh", "-eu", "-c", script, "recipe", "argument with spaces"],
        env={**os.environ, "runtime_dir": str(runtime)}, capture_output=True, text=True, timeout=90,
    )


def _state(root):
    return {str(p.relative_to(root)): (stat.S_IMODE(p.stat().st_mode), p.read_bytes() if p.is_file() else None)
            for p in (root, *root.rglob("*"))}


# A real dependency from a hash-checked local wheel imports before refresh;
# removing it from the lock and rerunning the exact recipe removes it. Both
# cache choices, repeated setup and read-only runtime/argument preservation are
# exercised without network, concurrent setup or an interpreter-version change.
@pytest.mark.parametrize("xdg", [True, False])
def test_recipe_removes_dependency_from_previous_lock(tmp_path, monkeypatch, xdg):
    setup, invoke = _recipe()
    venv = _environment(tmp_path, monkeypatch, xdg)
    wheel = tmp_path / "recipe_fixture-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("recipe_fixture.py", 'VALUE = "installed-from-lock"\n')
        archive.writestr("recipe_fixture-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: recipe-fixture\nVersion: 1.0\n")
        archive.writestr("recipe_fixture-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("recipe_fixture-1.0.dist-info/RECORD", "")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    runtimes = [tmp_path / "runtime old", tmp_path / "runtime new"]
    for index, root in enumerate(runtimes):
        root.mkdir()
        (root / "requirements.lock").write_text(
            f"recipe-fixture @ {wheel.as_uri()} --hash=sha256:{digest}\n" if index == 0 else ""
        )
        (root / "main.py").write_text("import sys; print(sys.prefix); print(sys.argv[1])\n")
        for file in root.iterdir():
            file.chmod(0o444)
        root.chmod(0o555)
    before = [_state(root) for root in runtimes]
    try:
        first = _run(setup, runtimes[0])
        assert first.returncode == 0, first.stderr
        python = str(venv / "bin/python")
        imported = subprocess.run([python, "-c", "import recipe_fixture; print(recipe_fixture.VALUE)"], capture_output=True, text=True)
        assert imported.returncode == 0 and imported.stdout.strip() == "installed-from-lock"
        for _ in range(2):
            refresh = _run(setup, runtimes[1])
            assert refresh.returncode == 0, refresh.stderr
            absent = subprocess.run([python, "-c", "import importlib.util; assert importlib.util.find_spec('recipe_fixture') is None"], capture_output=True, text=True)
            assert absent.returncode == 0, absent.stderr
            invoked = _run(invoke, runtimes[1])
            assert invoked.returncode == 0, invoked.stderr
            assert invoked.stdout.splitlines() == [str(venv), "argument with spaces"]
        assert (venv / "bin/python").is_symlink()
        assert [_state(root) for root in runtimes] == before
    finally:
        for root in runtimes:
            root.chmod(0o755)


# Clearing a symlinked environment must stop before touching its target; the
# ordinary real-directory control is exercised by the recreation test above.
def test_recipe_refuses_symlinked_environment(tmp_path, monkeypatch):
    setup, _ = _recipe()
    venv = _environment(tmp_path, monkeypatch, True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("preserved")
    venv.parent.mkdir(parents=True)
    venv.symlink_to(outside, target_is_directory=True)
    before = _state(outside)
    refused = _run(setup, tmp_path)
    assert refused.returncode == 1
    assert "Refusing to clear a symlinked venv directory" in refused.stderr
    assert venv.is_symlink() and _state(outside) == before
