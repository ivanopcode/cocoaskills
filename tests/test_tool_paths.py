from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
from io import BytesIO, StringIO
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def resolver():
    from csk.tool_paths import resolve_tool

    resolve_tool.cache_clear()
    yield resolve_tool
    resolve_tool.cache_clear()


def _executable(directory: Path, name: str = "git") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + ".exe" if os.name == "nt" else name)
    path.write_bytes(b"fixture")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("consumer", ["git_ops", "gitignore_gate", "cli", "project_resolver"])
@pytest.mark.parametrize("location", ["project", "global", "published"])
def test_shadowing_git_is_not_executed(tmp_path: Path, consumer: str, location: str) -> None:
    directories = {
        "project": tmp_path / "project" / ".agents" / "bin",
        "global": tmp_path / ".cocoaskills" / "global" / "bin",
        "published": tmp_path / "published",
    }
    directory = directories[location]
    directory.mkdir(parents=True)
    if location == "published":
        (directory / ".csk-managed.json").write_text("{}", encoding="utf-8")
    marker = tmp_path / "shadow-ran"
    shadow = directory / ("git.cmd" if os.name == "nt" else "git")
    if os.name == "nt":
        shadow.write_text('@echo off\necho shadow>"%CSK_TEST_MARKER%"\necho true\n', encoding="utf-8")
    else:
        shadow.write_text('#!/bin/sh\n: > "$CSK_TEST_MARKER"\nprintf "true\\n"\n', encoding="utf-8")
        shadow.chmod(0o755)
    snippets = {
        "git_ops": "git_ops.git(root, ['--version'])",
        "gitignore_gate": "gitignore_gate.missing_entries(root, ['.agents/'])",
        "cli": "cli._is_inside_git_worktree(root)",
        "project_resolver": "project_resolver.git_branch(root)",
    }
    # A fresh manager process exercises resolution with the hostile PATH from
    # entry, independently of any resolver cache populated by other tests.
    code = (
        "import sys; from pathlib import Path; "
        "from csk import git_ops, gitignore_gate, cli, project_resolver; "
        "root = Path(sys.argv[1]); " + snippets[consumer]
    )
    environment = dict(os.environ)
    environment.update(
        PATH=str(directory) + os.pathsep + os.environ.get("PATH", ""),
        PYTHONPATH=str(SOURCE_ROOT),
        CSK_TEST_MARKER=str(marker),
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=environment, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert not marker.exists(), "manager executed the shadowing git"


def test_no_bare_git_or_ssh_add_argv() -> None:
    # Grep argv literals and the former optional-git fallback throughout csk.
    pattern = re.compile(r'''(?<![\w.])(?:\[|\()\s*["'](?:git|ssh-add)["']\s*(?:,|[\]\)])|\bgit\s+or\s+["']git["']''')
    matches = []
    for path in (SOURCE_ROOT / "csk").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and '"git", tool.executable, False' not in line:
                matches.append(f"{path.relative_to(SOURCE_ROOT)}:{number}")
    assert not matches, matches


@pytest.mark.parametrize("consumer", ["git_ops", "gitignore_gate", "cli", "project_resolver"])
@pytest.mark.parametrize("location", ["project", "global", "published", "custom"])
def test_case_alias_shim_git_is_not_executed(tmp_path: Path, consumer: str, location: str) -> None:
    _assert_case_alias_shim_git_is_not_executed(tmp_path, consumer, location, nested=False)


@pytest.mark.parametrize("consumer", ["git_ops", "gitignore_gate", "cli", "project_resolver"])
@pytest.mark.parametrize("location", ["project", "global", "published", "custom"])
def test_nested_case_alias_shim_git_is_not_executed(tmp_path: Path, consumer: str, location: str) -> None:
    _assert_case_alias_shim_git_is_not_executed(tmp_path, consumer, location, nested=True)


def _assert_case_alias_shim_git_is_not_executed(
    tmp_path: Path, consumer: str, location: str, *, nested: bool,
) -> None:
    directories = {
        "project": tmp_path / "project" / ".agents" / "bin",
        "global": tmp_path / ".cocoaskills" / "global" / "bin",
        "published": tmp_path / "published",
        "custom": tmp_path / "manager" / "global" / "bin",
    }
    aliases = {
        "project": tmp_path / "project" / ".AGENTS" / "BIN",
        "global": tmp_path / ".COCOASKILLS" / "GLOBAL" / "BIN",
        "published": tmp_path / "PUBLISHED",
        "custom": tmp_path / "MANAGER" / "GLOBAL" / "BIN",
    }
    directory = directories[location]
    directory.mkdir(parents=True)
    alias = aliases[location]
    if not alias.exists() or not alias.samefile(directory):
        pytest.skip("filesystem has no case alias for this shim directory")
    if location == "published":
        (directory / ".csk-managed.json").write_text("{}", encoding="utf-8")
    if nested:
        directory = directory / "nested"
        directory.mkdir()
        alias = alias / "nested"
    marker = tmp_path / "shadow-ran"
    shadow = directory / ("git.cmd" if os.name == "nt" else "git")
    if os.name == "nt":
        shadow.write_text('@echo off\necho shadow>"%CSK_TEST_MARKER%"\necho true\n', encoding="utf-8")
    else:
        shadow.write_text('#!/bin/sh\n: > "$CSK_TEST_MARKER"\nprintf "true\\n"\n', encoding="utf-8")
        shadow.chmod(0o755)
    snippets = {
        "git_ops": "git_ops.git(root, ['--version'])",
        "gitignore_gate": "gitignore_gate.missing_entries(root, ['.agents/'])",
        "cli": "cli._is_inside_git_worktree(root)",
        "project_resolver": "project_resolver.git_branch(root)",
    }
    code = (
        "import sys; from pathlib import Path; "
        "from csk import git_ops, gitignore_gate, cli, project_resolver; "
        "root = Path(sys.argv[1]); " + snippets[consumer]
    )
    environment = {
        **os.environ,
        "PATH": str(alias) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(SOURCE_ROOT),
        "CSK_TEST_MARKER": str(marker),
        "CSK_CONFIG": str(tmp_path / "manager" / "config.json"),
    }
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        env=environment, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert not marker.exists(), "manager executed the case-alias shim git"


@pytest.mark.parametrize("name", ["git", "ssh-add"])
@pytest.mark.parametrize("location", ["project", "global", "published", "custom"])
def test_resolver_skips_shim_subdirectories(tmp_path, monkeypatch, resolver, name, location):
    directories = {
        "project": tmp_path / "project" / ".agents" / "bin",
        "global": tmp_path / ".cocoaskills" / "global" / "bin",
        "published": tmp_path / "published",
        "custom": tmp_path / "manager" / "global" / "bin",
    }
    root = directories[location]
    root.mkdir(parents=True)
    if location == "published":
        (root / ".csk-managed.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "manager" / "config.json"))
    shadow = _executable(root / "nested", name)
    real = _executable(tmp_path / "tools", name)
    monkeypatch.setenv("PATH", os.pathsep.join([str(shadow.parent), str(real.parent)]))
    assert resolver(name) == str(real.resolve())


@pytest.mark.parametrize("alias_kind", ["directory", "executable", "shim_entry"])
def test_resolver_checks_lexical_and_resolved_paths(tmp_path, monkeypatch, resolver, alias_kind):
    shim_dir = tmp_path / "project" / ".agents" / "bin"
    shadow = _executable(shim_dir)
    real = _executable(tmp_path / "tools")
    alias = tmp_path / "alias"
    try:
        if alias_kind == "directory":
            alias.symlink_to(shim_dir, target_is_directory=True)
            first = alias
        elif alias_kind == "executable":
            alias.mkdir()
            (alias / shadow.name).symlink_to(shadow)
            first = alias
        else:
            shadow.unlink()
            shadow.symlink_to(real)
            first = shim_dir
    except OSError:
        if os.name == "nt":
            pytest.skip("symlink creation unavailable")
        raise
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(real.parent)]))
    if alias_kind == "shim_entry":
        # Prove the lexical entry is excluded even if its target is admissible.
        with pytest.raises(FileNotFoundError):
            resolver("git", search_path=str(first))
    assert resolver("git") == str(real.resolve())


@pytest.mark.parametrize("entry", ["", ".", "relative"])
def test_resolver_skips_relative_path_entries(tmp_path, monkeypatch, resolver, entry):
    monkeypatch.chdir(tmp_path)
    _executable(tmp_path / entry)
    real = _executable(tmp_path / "tools")
    monkeypatch.setenv("PATH", os.pathsep.join([entry, str(real.parent)]))
    assert resolver("git") == str(real.resolve())


def test_resolver_skips_non_regular_files(tmp_path, monkeypatch, resolver):
    real = _executable(tmp_path / "tools")
    bad = tmp_path / "bad"
    (bad / real.name).mkdir(parents=True)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bad), str(real.parent)]))
    assert resolver("git") == str(real.resolve())


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission")
def test_resolver_skips_non_executable_files(tmp_path, monkeypatch, resolver):
    bad = _executable(tmp_path / "bad")
    bad.chmod(0o644)
    real = _executable(tmp_path / "tools")
    monkeypatch.setenv("PATH", os.pathsep.join([str(bad.parent), str(real.parent)]))
    assert resolver("git") == str(real.resolve())


def test_resolver_freezes_default_resolution(tmp_path, monkeypatch, resolver):
    first = _executable(tmp_path / "first")
    second = _executable(tmp_path / "second")
    monkeypatch.setenv("PATH", str(first.parent))
    assert resolver("git") == str(first.resolve())
    monkeypatch.setenv("PATH", str(second.parent))
    assert resolver("git") == str(first.resolve())


@pytest.mark.parametrize("kind", ["relative", "directory", "shim", "missing"])
def test_explicit_tool_path_is_admitted(tmp_path, monkeypatch, resolver, kind):
    monkeypatch.chdir(tmp_path)
    relative_tool = _executable(tmp_path)
    paths = {
        "relative": Path(relative_tool.name),
        "directory": tmp_path,
        "shim": _executable(tmp_path / ".agents" / "bin"),
        "missing": tmp_path / "missing",
    }
    with pytest.raises(FileNotFoundError):
        resolver("git", executable=str(paths[kind]))


@pytest.mark.parametrize("consumer", ["https", "ssh", "installer"])
def test_probes_use_absolute_tools(tmp_path, monkeypatch, resolver, consumer):
    from csk import build_https, build_ssh, installer
    from csk.builds.toolchain import OperatorSearchPath

    real_git = _executable(tmp_path / "tools")
    real_ssh_add = _executable(tmp_path / "tools", "ssh-add")
    _executable(tmp_path / ".agents" / "bin")
    _executable(tmp_path / ".agents" / "bin", "ssh-add")
    path = os.pathsep.join([str(tmp_path / ".agents" / "bin"), str(real_git.parent)])
    monkeypatch.setenv("PATH", path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(tuple(argv))
        stdout = "fixture key\n" if kwargs.get("text") else b"username=fixture\npassword=fixture\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    if consumer == "https":
        assert build_https.read_host_credentials("fixture.test", home=str(tmp_path)) == ("fixture", "fixture")
        assert calls == [(str(real_git.resolve()), "credential", "fill")]
    elif consumer == "ssh":
        found = build_ssh.discover_candidates(
            environment={"PATH": path, "SSH_AUTH_SOCK": "fixture-socket"}, home=str(tmp_path),
        )
        assert found.agent_key_count == 1
        assert calls == [(str(real_ssh_add.resolve()), "-l")]
    else:
        assert installer._operator_program("git", OperatorSearchPath(tuple(path.split(os.pathsep)))) == real_git.resolve()
        assert not calls


def test_missing_tools_fail_without_running_a_child(tmp_path, monkeypatch, resolver):
    from csk import build_https, build_ssh, git_ops

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran unresolved tool"))
    with pytest.raises(git_ops.GitError, match="git executable not found"):
        git_ops.git(tmp_path, ["--version"])
    assert build_https.read_host_credentials("fixture.test", home=str(tmp_path)) is None
    assert build_ssh.discover_candidates(
        environment={"PATH": str(tmp_path), "SSH_AUTH_SOCK": "fixture-socket"}, home=str(tmp_path),
    ).agent_key_count is None


@pytest.mark.parametrize("operation", ["git", "clone", "archive"])
def test_git_ops_use_absolute_git(tmp_path, monkeypatch, resolver, operation):
    from csk import git_ops

    executable = _executable(tmp_path / "tools")
    monkeypatch.setenv("PATH", str(executable.parent))
    archive = BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(tuple(argv))
        stdout = "" if kwargs.get("text") else archive.getvalue()
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    (tmp_path / ".git").mkdir()
    if operation == "git":
        git_ops.git(tmp_path, ["--version"])
    elif operation == "clone":
        git_ops.clone_repo(str(tmp_path / "source"), tmp_path / "destination")
    else:
        git_ops.archive(tmp_path, "a" * 40, tmp_path / "snapshot")
    assert len(calls) == 1
    assert calls[0][0] == str(executable.resolve())


@pytest.mark.parametrize("command", ["list", "login", "remove"])
def test_cli_passes_resolved_git_to_https(tmp_path, monkeypatch, resolver, command):
    from csk import build_https, cli, config

    executable = _executable(tmp_path / "tools")
    monkeypatch.setenv("PATH", str(executable.parent))
    cfg = config.parse_config({
        "schema_version": 1, "skills_root": str(tmp_path / "skills"),
        "default_agents": ["claude_code"], "projects": {},
        "build_https": {"fixture.test": {"token": "keyring"}},
    }, tmp_path / "config.json")
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(config, "save_config", lambda cfg: None)
    monkeypatch.setattr(build_https, "resolve_operator_home", lambda: str(tmp_path))
    calls = []

    def fake_credential(*args, **kwargs):
        calls.append(kwargs["git"])
        return "fixture"

    for name in ("read_namespaced_token", "store_namespaced_token", "delete_namespaced_token"):
        monkeypatch.setattr(build_https, name, fake_credential)
    monkeypatch.setattr(sys, "stdin", StringIO("fixture\n"))
    argv = ["config", "build-https", command]
    if command != "list":
        argv.append("fixture.test")
    assert cli.main(argv) == cli.EXIT_OK
    assert calls == [str(executable.resolve())] * (2 if command == "remove" else 1)


def test_cli_reports_missing_git(tmp_path, monkeypatch, resolver, capsys):
    from csk import cli

    monkeypatch.setenv("PATH", str(tmp_path))
    assert cli.main(["init", str(tmp_path)]) == cli.EXIT_CONFIG
    assert "git executable not found" in capsys.readouterr().err
