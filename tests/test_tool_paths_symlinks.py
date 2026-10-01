from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from csk import shims
from csk.tool_paths import resolve_tool


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def clear_resolver_cache():
    resolve_tool.cache_clear()
    yield
    resolve_tool.cache_clear()


def _link(link: Path, target: Path, *, relative: bool = False, directory: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(os.path.relpath(target, link.parent) if relative else target,
                        target_is_directory=directory)
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows runner cannot create symlinks")
        raise


def _shim_root(tmp_path: Path, location: str) -> Path:
    roots = {
        "project": tmp_path / "project" / ".agents" / "bin",
        "global": tmp_path / ".cocoaskills" / "global" / "bin",
        "custom": tmp_path / "manager" / "global" / "bin",
        "published": tmp_path / "published",
    }
    root = roots[location]
    root.mkdir(parents=True, exist_ok=True)
    if location == "published":
        (root / ".csk-managed.json").write_text("{}", encoding="utf-8")
    return root


def _regular_tool(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="actual POSIX symlink launcher execution")
@pytest.mark.parametrize("location", ["project", "global", "custom", "published"])
@pytest.mark.parametrize("alias_kind", ["directory", "executable", "relative", "intermediate-directory", "directory-chain"])
def test_symlinked_real_shim_is_never_executed(tmp_path, location, alias_kind):
    """Keep the rev1 reviewer regression name and use the actual shim writer."""
    marker = tmp_path / "executed"
    runtime = tmp_path / "runtime" / "provider" / "tool"
    runtime.parent.mkdir(parents=True)
    runtime.write_text('#!/bin/sh\n: > "$CSK_TEST_MARKER"\nprintf "shadow\\n"\n', encoding="utf-8")
    runtime.chmod(0o755)
    root = _shim_root(tmp_path, location)
    shim = shims.write_bin_shim(root, "git", runtime)
    assert shim.is_symlink(), "actual POSIX shim shape is required"
    if alias_kind == "directory-chain":
        external = tmp_path / "external" / "provider" / "bin"
        if location == "published":
            external = tmp_path / "published-target"
        external.parent.mkdir(parents=True, exist_ok=True)
        root.rename(external)
        _link(root, external, directory=True)
    alias = tmp_path / "alias"
    if alias_kind in ("directory", "directory-chain"):
        _link(alias, root, directory=True)
    else:
        if alias_kind == "intermediate-directory":
            directory_alias = tmp_path / "hidden-bin"
            _link(directory_alias, root, directory=True)
            target = directory_alias / "git"
        else:
            target = shim
        _link(alias / "git", target, relative=alias_kind == "relative")
    environment = {
        **os.environ,
        "PATH": str(alias) + os.pathsep + os.defpath,
        "PYTHONPATH": str(SOURCE_ROOT),
        "CSK_TEST_MARKER": str(marker),
        "CSK_CONFIG": str(tmp_path / "manager" / "config.json"),
    }
    run = subprocess.run(
        [sys.executable, "-c", 'from pathlib import Path; from csk import git_ops; '
         'git_ops.git(Path.cwd(), ["--version"])'],
        cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert not marker.exists(), f"manager executed {location} shim through {alias_kind} alias"


@pytest.mark.skipif(os.name == "nt", reason="actual POSIX symlink launcher execution")
@pytest.mark.parametrize("consumer", ["https", "ssh"])
def test_credential_probe_never_executes_aliased_shim(tmp_path, consumer):
    marker = tmp_path / "forbidden-child"
    runtime = tmp_path / "runtime" / "payload"
    runtime.parent.mkdir()
    runtime.write_text('#!/bin/sh\n: > "$CSK_TEST_MARKER"\n'
                       'printf "username=fixture\\npassword=fixture\\n"\n', encoding="utf-8")
    runtime.chmod(0o755)
    name = "git" if consumer == "https" else "ssh-add"
    shim = shims.write_project_shim(tmp_path / "project", name, runtime)
    _link(tmp_path / "alias" / name, shim)
    # Do not touch the operator's credential helper or SSH agent. The admitted
    # synthetic tool consumes the credential protocol and exits successfully.
    trusted = _regular_tool(tmp_path / "trusted" / name)
    trusted.write_text('#!/bin/sh\nprintf "username=fixture\\npassword=fixture\\n"\n', encoding="utf-8")
    snippets = {
        "https": 'from csk import build_https; '
                 'build_https.store_namespaced_token("fixture.test", "fixture.test", '
                 '"fixture", home=sys.argv[1])',
        "ssh": 'from csk import build_ssh; '
               'build_ssh.discover_candidates(environment={"PATH":os.environ["PATH"], '
               '"SSH_AUTH_SOCK":"nonexistent-fixture-socket"}, home=sys.argv[1])',
    }
    environment = {
        **os.environ, "PATH": os.pathsep.join([str(tmp_path / "alias"), str(trusted.parent)]),
        "PYTHONPATH": str(SOURCE_ROOT), "CSK_TEST_MARKER": str(marker),
        "CSK_CONFIG": str(tmp_path / "manager" / "config.json"),
    }
    run = subprocess.run(
        [sys.executable, "-c", "import sys, os; " + snippets[consumer], str(tmp_path)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert not marker.exists(), f"{consumer} credential consumer executed a csk shim"


@pytest.mark.parametrize("location", ["project", "global", "custom", "published"])
@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("entry", ["path", "explicit", "directory", "intermediate-directory"])
def test_resolver_refuses_intermediate_shim(tmp_path, monkeypatch, location, relative, entry):
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "manager" / "config.json"))
    target = _regular_tool(tmp_path / "runtime" / "target")
    root = _shim_root(tmp_path, location)
    name = "git.exe" if os.name == "nt" else "git"
    _link(root / name, target, relative=relative)
    alias = tmp_path / "alias"
    if entry == "directory":
        _link(alias, root, directory=True)
    else:
        intermediate = root / name
        if entry == "intermediate-directory":
            _link(tmp_path / "hidden-bin", root, directory=True)
            intermediate = tmp_path / "hidden-bin" / name
        _link(alias / name, intermediate, relative=relative)
    with pytest.raises(FileNotFoundError):
        if entry == "explicit":
            resolve_tool("git", executable=str(alias / name))
        else:
            resolve_tool("git", search_path=str(alias))


@pytest.mark.parametrize("links", [1, 3, 40])
def test_resolver_admits_legitimate_relative_chain(tmp_path, links):
    target = _regular_tool(tmp_path / "Cellar" / "git" / "version" / "tool")
    name = "git.exe" if os.name == "nt" else "git"
    for number in reversed(range(links)):
        directory = tmp_path / "bin" if number == 0 else tmp_path / "links" / f"step-{number}" / "bin"
        link = directory / name
        _link(link, target, relative=True)
        target = link
    expected = str((tmp_path / "Cellar" / "git" / "version" / "tool").resolve())
    assert resolve_tool("git", search_path=str(tmp_path / "bin")) == expected
    assert resolve_tool("git", executable=str(target)) == expected


def test_resolver_refuses_symlink_overflow(tmp_path):
    target = _regular_tool(tmp_path / "runtime" / "tool")
    for number in reversed(range(41)):
        link = tmp_path / f"link-{number}"
        _link(link, target, relative=True)
        target = link
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", executable=str(target))


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission")
def test_resolver_refuses_nonexecutable_chain_target(tmp_path):
    target = _regular_tool(tmp_path / "runtime" / "tool")
    target.chmod(0o644)
    link = tmp_path / "alias"
    _link(link, target)
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", executable=str(link))


@pytest.mark.parametrize("location", ["project", "global", "custom", "published"])
def test_resolver_refuses_chained_directory_alias(tmp_path, monkeypatch, location):
    """An alias can pass through a managed directory link before reaching disk."""
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "manager" / "config.json"))
    root = _shim_root(tmp_path, location)
    external = tmp_path / "external" / "provider" / "bin"
    external.mkdir(parents=True)
    # Keep the two directory locations at the same depth so a POSIX shim's
    # relative target remains a valid executable, rather than a dangling link.
    if location == "published":
        external = tmp_path / "published-target"
        external.mkdir()
        (root / ".csk-managed.json").unlink()
    root.rmdir()
    _link(root, external, directory=True)
    if location == "published":
        (root / ".csk-managed.json").write_text("{}", encoding="utf-8")
    target = _regular_tool(tmp_path / "runtime" / "tool")
    # Force the POSIX writer on every host: directory alias resolution is
    # cross-platform even when native command launchers are .cmd files.
    shim = shims.write_bin_shim(root, "git", target, platform_name="unix")
    name = "git.exe" if os.name == "nt" else "git"
    if name != shim.name:
        shim.rename(root / name)
    _link(tmp_path / "alias", root, directory=True)
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", search_path=str(tmp_path / "alias"))


@pytest.mark.parametrize("relative", [False, True])
def test_resolver_admits_legitimate_directory_alias(tmp_path, relative):
    name = "git.exe" if os.name == "nt" else "git"
    target = _regular_tool(tmp_path / "tools" / name)
    _link(tmp_path / "alias", target.parent, relative=relative, directory=True)
    admitted = Path(resolve_tool("git", search_path=str(tmp_path / "alias")))
    assert admitted.is_absolute()
    assert admitted.samefile(target)


@pytest.mark.parametrize("location", ["project", "global", "custom", "published"])
def test_resolver_refuses_directory_link_in_managed_ancestor(tmp_path, monkeypatch, location):
    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "manager" / "config.json"))
    name = "git.exe" if os.name == "nt" else "git"
    tool = _regular_tool(tmp_path / "outside" / name)
    root = _shim_root(tmp_path, location)
    _link(root / "nested", tool.parent, directory=True)
    _link(tmp_path / "alias", root / "nested", directory=True)
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", search_path=str(tmp_path / "alias"))


@pytest.mark.parametrize("kind", ["broken", "read-error"])
def test_resolver_refuses_unknown_managed_ledger(tmp_path, monkeypatch, kind):
    tmp_path = tmp_path.resolve()
    name = "git.exe" if os.name == "nt" else "git"
    tool = _regular_tool(tmp_path / "tools" / name)
    ledger = tool.parent / ".csk-managed.json"
    if kind == "broken":
        _link(ledger, tmp_path / "missing-ledger")
    else:
        original = Path.lstat

        def denied(path, *args, **kwargs):
            if Path(path) == ledger:
                raise PermissionError("fixture ledger lookup denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", search_path=str(tool.parent))


@pytest.mark.parametrize("links", [40, 41])
def test_resolver_bounds_directory_link_chain(tmp_path, links):
    name = "git.exe" if os.name == "nt" else "git"
    tool = _regular_tool(tmp_path / "tools" / name)
    target = tool.parent
    for number in reversed(range(links)):
        link = tmp_path / "directories" / f"link-{number}"
        _link(link, target, relative=True, directory=True)
        target = link
    if links == 40:
        assert resolve_tool("git", search_path=str(target)) == str(tool.resolve())
    else:
        with pytest.raises(FileNotFoundError):
            resolve_tool("git", search_path=str(target))


@pytest.mark.parametrize("file_links", [39, 40])
def test_resolver_shares_link_budget_with_directory_alias(tmp_path, file_links):
    target = _regular_tool(tmp_path / "runtime" / "tool")
    name = "git.exe" if os.name == "nt" else "git"
    for number in reversed(range(file_links)):
        link = tmp_path / "tools" / (name if number == 0 else f"link-{number}")
        _link(link, target, relative=True)
        target = link
    _link(tmp_path / "alias", target.parent, relative=True, directory=True)
    if file_links == 39:
        assert resolve_tool("git", search_path=str(tmp_path / "alias")) == str((tmp_path / "runtime" / "tool").resolve())
    else:
        with pytest.raises(FileNotFoundError):
            resolve_tool("git", search_path=str(tmp_path / "alias"))


@pytest.mark.skipif(os.name != "nt", reason="native Windows directory junctions")
def test_resolver_refuses_junction_chain_through_shim_directory(tmp_path):
    target = _regular_tool(tmp_path / "outside" / "git.exe")
    shim_dir = tmp_path / "project" / ".agents" / "bin"
    shim_dir.parent.mkdir(parents=True)
    alias = tmp_path / "alias"
    for link, directory in [(shim_dir, target.parent), (alias, shim_dir)]:
        result = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J", str(link), str(directory)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", search_path=str(alias))


def test_resolver_refuses_directory_loop_without_revisiting_links(tmp_path, monkeypatch):
    # One filename candidate, so Windows PATHEXT retries do not count as
    # revisiting a link inside a single admission attempt.
    monkeypatch.setenv("PATHEXT", ".EXE")
    tmp_path = tmp_path.resolve()
    first, second = tmp_path / "first", tmp_path / "second"
    _link(first, second, relative=True, directory=True)
    _link(second, first, relative=True, directory=True)
    readlink = os.readlink
    calls = []

    def counted(path, *args, **kwargs):
        calls.append(Path(path))
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", counted)
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", search_path=str(first))
    assert [path for path in calls if path in (first, second)] == [first, second]


@pytest.mark.parametrize("operation", ["lstat", "readlink"])
def test_resolver_refuses_directory_read_failure(tmp_path, monkeypatch, operation):
    tmp_path = tmp_path.resolve()
    name = "git.exe" if os.name == "nt" else "git"
    tool = _regular_tool(tmp_path / "tools" / name)
    alias = tmp_path / "alias"
    _link(alias, tool.parent, directory=True)
    owner = Path if operation == "lstat" else os
    original = getattr(owner, operation)

    def denied(path, *args, **kwargs):
        if Path(path) == alias:
            raise PermissionError("fixture directory lookup denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(owner, operation, denied)
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", search_path=str(alias))


def test_resolver_refuses_loop_without_revisiting_links(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    first, second = tmp_path / "first", tmp_path / "second"
    _link(first, second, relative=True)
    _link(second, first, relative=True)
    readlink = os.readlink
    calls = []

    def counted(path, *args, **kwargs):
        calls.append(Path(path))
        return readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", counted)
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", executable=str(first))
    # Directory realpath checks may read links in the temp prefix. Only count
    # the candidate links, to prove the cycle guard stops at the first repeat.
    assert [path for path in calls if path in (first, second)] == [first, second]


@pytest.mark.parametrize("failure", ["missing", "directory", "unreadable-link", "unreadable-directory"])
def test_resolver_refuses_failed_chain_reads(tmp_path, monkeypatch, failure):
    tmp_path = tmp_path.resolve()
    target = tmp_path / "target"
    if failure != "missing":
        if failure == "directory":
            target.mkdir()
        else:
            _regular_tool(target)
    link = tmp_path / "alias"
    _link(link, target)
    if failure == "unreadable-link":
        original = os.readlink

        def denied(path, *args, **kwargs):
            if Path(path) == link:
                raise PermissionError("fixture readlink denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "readlink", denied)
    elif failure == "unreadable-directory":
        original = Path.lstat

        def denied_directory(path, *args, **kwargs):
            if Path(path) == link.parent:
                raise PermissionError("fixture directory identity denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", denied_directory)
    with pytest.raises(FileNotFoundError):
        resolve_tool("git", executable=str(link))
