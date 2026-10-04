"""Project-aware POSIX dispatch: generations, shims, dispatcher.

Slice TASK-261004-14uqq0, design part A. Every behavior here is driven
through a real ``csk install`` / ``csk global install`` plus real subprocess
bare calls; helper-level checks supplement but never replace that path.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest
from conftest import (
    commit_all,
    init_git_repo,
    make_config,
    make_project,
    make_skill_repo,
    run,
    write_files,
    write_skillfile,
)

from csk import _dispatch_runtime, cli, dispatch, global_install, installer, locking
from csk._dispatch_runtime import DispatchError
from csk.config import load_config, save_config


class _ExecveIntercepted(Exception):
    """Raised by the execve stub so a test can observe the dispatch tail."""


def _resolve_open_fd(fd: int) -> str | None:
    """Best-effort path for one open file descriptor, else None."""
    for template in ("/dev/fd/%d", "/proc/self/fd/%d"):
        try:
            return os.readlink(template % fd)
        except OSError:
            continue
    return None


def _normalize_traced_path(file: object, *, dir_fd: int | None = None) -> str:
    """Normalize one traced open/list path to a real absolute path.

    Bytes decode with surrogateescape, ``dir_fd``-relative paths resolve
    against the descriptor target, and anything else relative resolves
    against the CWD at event time. Unresolvable descriptor shapes return
    a ``<...>`` marker the tracer treats as a violation (dispatch never
    opens through descriptors, so any occurrence is hostile).
    """
    if isinstance(file, int):
        resolved = _resolve_open_fd(file)
        if resolved is None:
            return f"<unresolved-fd:{file}>"
        return os.path.realpath(resolved)
    if isinstance(file, (bytes, bytearray)):
        text = bytes(file).decode("utf-8", "surrogateescape")
    else:
        text = os.fspath(file)
        if isinstance(text, bytes):
            text = text.decode("utf-8", "surrogateescape")
    if not os.path.isabs(text):
        if dir_fd is not None:
            base = _resolve_open_fd(dir_fd)
            if base is None:
                return f"<unresolved-dir-fd:{dir_fd}:{text}>"
            text = os.path.join(base, text)
        else:
            text = os.path.join(os.getcwd(), text)
    return os.path.realpath(text)


class _CheckoutReadTracer:
    """Prove dispatch opens and lists nothing under a checkout.

    Function wrappers record ``open``/``io.open``/``os.open`` (with
    ``dir_fd``) plus ``os.listdir``/``os.scandir`` with event-time
    normalization; a ``sys.addaudithook`` backstop records the same
    events for any call path that bypasses the wrappers. ``stat`` is
    deliberately untracked (the existence oracle stays a stated
    bound), as are reads through fds opened before dispatch starts.
    """

    def __init__(self, checkout: Path) -> None:
        self.checkout = os.path.realpath(checkout)
        self.records: set[str] = set()
        self._originals: dict = {}

    def _record(self, file: object, *, dir_fd: int | None = None) -> None:
        self.records.add(_normalize_traced_path(file, dir_fd=dir_fd))

    def _audit_hook(self, event: str, args: tuple) -> None:
        if event in ("open", "os.listdir", "os.scandir") and args:
            self._record(args[0])

    def __enter__(self) -> "_CheckoutReadTracer":
        import builtins
        import io

        tracer = self
        original_open = builtins.open
        original_io_open = io.open
        original_os_open = os.open
        original_listdir = os.listdir
        original_scandir = os.scandir
        self._originals = {
            "open": original_open,
            "os.open": original_os_open,
        }

        def tracked_open(file: object, *args: object, **kwargs: object) -> object:
            tracer._record(file)
            return original_open(file, *args, **kwargs)  # type: ignore[arg-type]

        def tracked_os_open(
            path: object, flags: int, *args: object, **kwargs: object
        ) -> int:
            tracer._record(path, dir_fd=kwargs.get("dir_fd"))  # type: ignore[arg-type]
            return original_os_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        def tracked_listdir(path: object = ".") -> list:
            tracer._record(path)
            return original_listdir(path)  # type: ignore[arg-type]

        def tracked_scandir(path: object = ".") -> object:
            tracer._record(path)
            return original_scandir(path)  # type: ignore[arg-type]

        builtins.open = tracked_open  # type: ignore[assignment]
        io.open = tracked_open  # type: ignore[assignment]
        os.open = tracked_os_open  # type: ignore[assignment]
        os.listdir = tracked_listdir  # type: ignore[assignment]
        os.scandir = tracked_scandir  # type: ignore[assignment]
        sys.addaudithook(self._audit_hook)
        self._restore = (
            original_open,
            original_io_open,
            original_os_open,
            original_listdir,
            original_scandir,
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        import builtins
        import io

        (
            original_open,
            original_io_open,
            original_os_open,
            original_listdir,
            original_scandir,
        ) = self._restore
        builtins.open = original_open
        io.open = original_io_open
        os.open = original_os_open
        os.listdir = original_listdir
        os.scandir = original_scandir
        # The audit hook cannot be removed; it appends to this dead
        # tracer's set for the rest of the process (harmless: the set
        # is never read again and later tests use fresh tracers).

    def violations(self) -> list[str]:
        """Return every record under the checkout plus unresolved markers."""
        hits = sorted(
            record
            for record in self.records
            if record.startswith("<")
            or record == self.checkout
            or record.startswith(self.checkout + os.sep)
        )
        return hits

    def manager_reads(self, manager_home: Path) -> list[str]:
        """Return every record under the manager home."""
        home = os.path.realpath(manager_home)
        return sorted(
            record
            for record in self.records
            if not record.startswith("<")
            and (record == home or record.startswith(home + os.sep))
        )


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX dispatch slice; Windows dispatch is a separate task",
)


def _script_body(marker: str) -> str:
    return (
        "#!/bin/sh\n"
        f'echo "{marker}"\n'
        'echo "ROOT:${CSK_PROJECT_ROOT-unset}"\n'
    )


def _make_command_skill(
    skills_root: Path,
    name: str,
    markers: dict[str, str],
    *,
    tag: str,
) -> tuple[Path, str]:
    """Create a skill repo whose every command prints its marker plus ROOT."""
    commands = {
        command: {"type": "script", "unix_path": f"scripts/{command}"}
        for command in markers
    }
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps({"schema_version": 1, "commands": commands})
    }
    for command, marker in markers.items():
        files[f"scripts/{command}"] = _script_body(marker)
    return make_skill_repo(skills_root, name, files, tag=tag)


def _retitle_skill(
    repo: Path, markers: dict[str, str], *, tag: str
) -> str:
    """Publish a new tag of one skill with new markers per command."""
    commands = {
        command: {"type": "script", "unix_path": f"scripts/{command}"}
        for command in markers
    }
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps({"schema_version": 1, "commands": commands})
    }
    for command, marker in markers.items():
        files[f"scripts/{command}"] = _script_body(marker)
    write_files(repo, files)
    commit = commit_all(repo, f"version {tag}")
    run(["git", "tag", tag], repo)
    return commit


def _tag_commit(repo: Path, tag: str) -> str:
    return run(["git", "rev-parse", tag], repo).stdout.strip()


def _make_nested_project(path: Path) -> Path:
    init_git_repo(path)
    write_files(
        path,
        {
            ".gitignore": ".agents/\n.claude/skills/\n.codex/skills/\n"
            ".gemini/skills/\n.cursor/rules/\n",
        },
    )
    commit_all(path, "gitignore")
    return path


def _install_project(
    csk_home: Path,
    skills_root: Path,
    project: Path,
    declarations: list[dict[str, str]],
):
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": declarations,
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    results = installer.install(cfg)
    assert len(results) == 1
    assert not results[0].errors, results[0].errors
    return results[0]


def _install_global(
    csk_home: Path,
    skills_root: Path,
    project: Path,
    declarations: list[dict[str, str]],
    *,
    only: list[str] | None = None,
):
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": ["claude_code"],
                "skills": declarations,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    result = global_install.install(cfg, only=only)
    assert not result.errors, result.errors
    return result


def _shims_env(csk_home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Bare-call environment: shims appended, scope hints poisoned.

    Every bare call in this file runs with bogus ``PWD``,
    ``CSK_PROJECT_ROOT`` and ``CSK_CONFIG`` so a dispatcher that consults
    any of them fails its assertions.
    """
    env = os.environ.copy()
    shims = str(dispatch.shims_dir(csk_home))
    env["PATH"] = (
        f"{env['PATH']}{os.pathsep}{shims}" if env.get("PATH") else shims
    )
    env["PWD"] = "/bogus-pwd"
    env["CSK_PROJECT_ROOT"] = "/bogus-root"
    env["CSK_CONFIG"] = "/bogus-config.json"
    if extra:
        env.update(extra)
    return env


def _bare(
    command: str,
    *args: str,
    cwd: Path,
    csk_home: Path,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one bare command exactly as an interactive shell would.

    The ``sh -c 'exec "$@"'`` form preserves argv exactly and reports a
    missing shim as the shell does (exit 127). Setup appends shims to
    PATH, so every name used here must be absent from the base PATH;
    the guard below fails fast instead of shadowing the shim.
    """
    shadow = shutil.which(command, path=os.environ.get("PATH"))
    assert shadow is None, f"test command {command!r} is shadowed by {shadow}"
    return subprocess.run(
        ["sh", "-c", 'exec "$@"', "sh", command, *args],
        cwd=cwd,
        env=_shims_env(csk_home, env_extra),
        text=True,
        capture_output=True,
    )


def _read_registry(csk_home: Path) -> tuple[dict, str]:
    current = (
        (dispatch.dispatch_dir(csk_home) / "current").read_text(encoding="utf-8").strip()
    )
    raw = (
        dispatch.generations_dir(csk_home) / current / "registry.json"
    ).read_text(encoding="utf-8")
    return json.loads(raw), current


def _entry_for_root(registry: dict, root: Path) -> dict:
    """Return the project record registered for one root.

    Checkout ids are stable random identities (reused when the same
    directory re-registers), so tests locate records by root, never by
    a spelling-derived key.
    """
    matches = [
        entry
        for entry in registry["projects"].values()
        if entry["canonical_root"] == str(root)
    ]
    assert len(matches) == 1, (
        f"expected one record for {root}, found {len(matches)}"
    )
    return matches[0]


def test_install_publishes_dispatch_generation_and_posix_shims(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-tool", {"dtool": "DTOOL-V1"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-tool", "tag": "v1"}])

    registry, generation = _read_registry(csk_home)
    assert registry["schema_version"] == 1
    assert len(generation) == 32
    assert sorted(registry) == [
        "global",
        "projects",
        "schema_version",
        "shim_case_insensitive",
    ]
    assert registry["shim_case_insensitive"] in (True, False)
    assert len(registry["projects"]) == 1
    entry = next(iter(registry["projects"].values()))
    canonical = str(project.resolve())
    assert entry["canonical_root"] == canonical
    assert len(entry["checkout_id"]) == 64
    int(entry["checkout_id"], 16)
    assert entry["project_alias"] == "app"
    assert [pin["name"] for pin in entry["skills"]] == ["skill-tool"]
    assert entry["commands"]["dtool"]["owner"] == "skill-tool"
    assert entry["commands"]["dtool"]["kind"] == "script"
    target = Path(entry["commands"]["dtool"]["target"])
    assert target.is_absolute()
    assert target.is_relative_to(csk_home)
    assert target.is_file()
    assert os.access(target, os.X_OK)
    digest = entry["commands"]["dtool"]["digest"]
    assert digest["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    info = target.stat()
    assert sorted(digest) == ["mtime_ns", "sha256", "size", "st_ino"]
    assert digest["st_ino"] == str(info.st_ino)
    assert (digest["size"], digest["mtime_ns"]) == (
        str(info.st_size),
        str(info.st_mtime_ns),
    )
    root_info = Path(canonical).stat()
    assert entry["root_identity"] == {"st_ino": str(root_info.st_ino)}

    shim = dispatch.shims_dir(csk_home) / "dtool"
    assert shim.is_file()
    assert os.access(shim, os.X_OK)
    dispatcher = dispatch.dispatcher_path(csk_home)
    assert dispatcher.is_file()
    assert os.access(dispatcher, os.X_OK)
    runtime = dispatch.dispatcher_runtime_path(csk_home)
    assert runtime.is_file()

    # This slice keeps the legacy checkout surface alongside dispatch.
    assert (project / ".agents" / "bin" / "dtool").exists()


def test_bare_calls_run_each_projects_pinned_version(tmp_path, skills_root, csk_home):
    repo, _commit = _make_command_skill(
        skills_root, "skill-ver", {"dver": "VERSION-ONE"}, tag="v1"
    )
    _retitle_skill(repo, {"dver": "VERSION-TWO"}, tag="v2")
    proj_p = make_project(tmp_path, "proj-p")
    proj_q = make_project(tmp_path, "proj-q")
    _install_project(csk_home, skills_root, proj_p, [{"name": "skill-ver", "tag": "v1"}])
    _install_project(csk_home, skills_root, proj_q, [{"name": "skill-ver", "tag": "v2"}])

    registry, _generation = _read_registry(csk_home)
    assert len(registry["projects"]) == 2
    pins = {
        entry["canonical_root"]: [pin["commit"] for pin in entry["skills"]]
        for entry in registry["projects"].values()
    }
    assert pins[str(proj_p.resolve())] != pins[str(proj_q.resolve())]

    proc_p = _bare("dver", cwd=proj_p, csk_home=csk_home)
    assert proc_p.returncode == 0, proc_p.stderr
    assert proc_p.stdout.splitlines() == ["VERSION-ONE", f"ROOT:{proj_p.resolve()}"]

    proc_q = _bare("dver", cwd=proj_q, csk_home=csk_home)
    assert proc_q.returncode == 0, proc_q.stderr
    assert proc_q.stdout.splitlines() == ["VERSION-TWO", f"ROOT:{proj_q.resolve()}"]


def test_argv_passthrough_is_exact_and_exit_status_preserved(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    repo, _commit = _make_command_skill(
        skills_root, "skill-args", {"dargs": "ARGS-UNUSED"}, tag="v1"
    )
    write_files(
        repo,
        {
            "scripts/dargs": (
                "#!/bin/sh\n"
                "i=0\n"
                'for a in "$@"; do echo "ARG$i:$a"; i=$((i+1)); done\n'
            ),
            "scripts/dexit": "#!/bin/sh\nexit 42\n",
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 1,
                    "commands": {
                        "dargs": {"type": "script", "unix_path": "scripts/dargs"},
                        "dexit": {"type": "script", "unix_path": "scripts/dexit"},
                    },
                }
            ),
        },
    )
    commit_all(repo, "argv scripts")
    run(["git", "tag", "v2"], repo)
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-args", "tag": "v2"}]
    )

    tricky = ["a b", "", "--flag", "quo'te", "$HOME", "-n"]
    proc = _bare("dargs", *tricky, cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [f"ARG{i}:{value}" for i, value in enumerate(tricky)]

    failed = _bare("dexit", cwd=project, csk_home=csk_home)
    assert failed.returncode == 42


def test_outside_projects_global_version_runs_and_root_is_unset(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"ptool": "PROJECT-P"}, tag="v1")
    _make_command_skill(skills_root, "skill-g", {"gtool": "GLOBAL-G"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-g", "tag": "v1"}])

    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _bare("gtool", cwd=outside, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["GLOBAL-G", "ROOT:unset"]

    # The project root metadata stays even when the command falls back.
    fallback = _bare("gtool", cwd=project, csk_home=csk_home)
    assert fallback.returncode == 0, fallback.stderr
    assert fallback.stdout.splitlines() == ["GLOBAL-G", f"ROOT:{project.resolve()}"]

    scoped = _bare("ptool", cwd=project, csk_home=csk_home)
    assert scoped.returncode == 0, scoped.stderr
    assert scoped.stdout.splitlines()[0] == "PROJECT-P"


def test_no_global_gives_clean_unavailable_refusal(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"ptool": "PROJECT-P"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])

    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _bare("ptool", cwd=outside, csk_home=csk_home)
    assert proc.returncode == 127
    assert "unavailable" in proc.stderr
    assert "outside every registered project" in proc.stderr

    # No layer exports this name, so no shim exists and the shell itself
    # reports 127; the dispatcher never runs.
    unknown = _bare("nope-missing", cwd=project, csk_home=csk_home)
    assert unknown.returncode == 127
    assert "not found" in unknown.stderr


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "not-regular",
        "not-executable",
        "symlink-escape",
        "changed-executable-bytes",
        "same-size-rewritten-bytes",
    ],
)
def test_corrupt_pinned_target_refuses_without_global_fallback(
    tmp_path, skills_root, csk_home, damage
):
    project = make_project(tmp_path, "proj-p")
    repo, _commit = _make_command_skill(
        skills_root, "skill-p", {"ctool": "PROJECT-C"}, tag="v1"
    )
    _retitle_skill(repo, {"ctool": "GLOBAL-C"}, tag="v2")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v2"}])
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    target = Path(entry["commands"]["ctool"]["target"])

    if damage == "missing":
        target.unlink()
    elif damage == "not-regular":
        target.unlink()
        target.mkdir()
    elif damage == "not-executable":
        target.chmod(0o644)
    elif damage == "symlink-escape":
        checkout_decoy = project / "Skillfile.json"
        target.unlink()
        target.symlink_to(checkout_decoy)
    elif damage == "changed-executable-bytes":
        target.write_text('#!/bin/sh\necho "CORRUPT-PIN"\n', encoding="utf-8")
        target.chmod(0o755)
    elif damage == "same-size-rewritten-bytes":
        content = target.read_bytes()
        assert b"PROJECT-C" in content
        target.write_bytes(content.replace(b"PROJECT-C", b"PROJECT-X"))
        # Same size, provably different mtime: the tuple must bind
        # content age, not just length, on any timestamp granularity.
        recorded_ns = int(entry["commands"]["ctool"]["digest"]["mtime_ns"])
        info = target.stat()
        os.utime(target, ns=(info.st_atime_ns, recorded_ns + 2_000_000_000))

    proc = _bare("ctool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "GLOBAL-C" not in proc.stdout
    assert "PROJECT-C" not in proc.stdout
    assert "CORRUPT-PIN" not in proc.stdout
    assert "PROJECT-X" not in proc.stdout
    assert "pinned by the project" in proc.stderr
    assert "never substituted" in proc.stderr
    assert "reinstall" in proc.stderr.lower()


def test_hostile_checkout_bin_and_env_never_execute(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-h", {"htool": "LEGIT-HTOOL"}, tag="v1")
    _make_command_skill(skills_root, "skill-g", {"gtool": "GLOBAL-G"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-h", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-g", "tag": "v1"}])

    # A hostile repository plants its own bin entry and env hook. The
    # checkout copy is hostile bytes; the manager runtime copy is legit.
    checkout_shim = project / ".agents" / "bin" / "htool"
    assert checkout_shim.exists()
    if checkout_shim.is_symlink() or checkout_shim.is_file():
        checkout_shim.unlink()
    checkout_shim.write_text('#!/bin/sh\necho "HOSTILE-BIN"\n', encoding="utf-8")
    checkout_shim.chmod(0o755)
    env_marker = project / ".agents" / "env-marker"
    (project / ".agents" / "env.sh").write_text(
        f'#!/bin/sh\necho "HOSTILE-ENV"\ntouch "{env_marker}"\n', encoding="utf-8"
    )

    proc = _bare("htool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "LEGIT-HTOOL"
    assert "HOSTILE-BIN" not in proc.stdout
    assert "HOSTILE-ENV" not in proc.stdout
    assert not env_marker.exists()

    # An unregistered hostile checkout cannot shadow the global layer either.
    hostile = tmp_path / "hostile"
    (hostile / ".agents" / "bin").mkdir(parents=True)
    (hostile / ".agents" / "bin" / "gtool").write_text(
        '#!/bin/sh\necho "HOSTILE-BIN"\n', encoding="utf-8"
    )
    (hostile / ".agents" / "bin" / "gtool").chmod(0o755)
    (hostile / ".agents" / "env.sh").write_text(
        '#!/bin/sh\necho "HOSTILE-ENV"\n', encoding="utf-8"
    )
    outside = _bare("gtool", cwd=hostile, csk_home=csk_home)
    assert outside.returncode == 0, outside.stderr
    assert outside.stdout.splitlines()[0] == "GLOBAL-G"


def test_symlinked_path_to_same_root_matches(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"stool": "SYMLINK-P"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])

    link = tmp_path / "proj-link"
    link.symlink_to(project, target_is_directory=True)
    proc = _bare("stool", cwd=link, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["SYMLINK-P", f"ROOT:{project.resolve()}"]


def test_string_prefix_sibling_is_not_a_project_match(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj")
    repo, _commit = _make_command_skill(
        skills_root, "skill-p", {"etool": "PREFIX-P"}, tag="v1"
    )
    _retitle_skill(repo, {"etool": "PREFIX-G"}, tag="v2")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v2"}])

    # "/proj-evil" starts with "/proj" as a string but is not beneath it.
    sibling = tmp_path / "proj-evil"
    sibling.mkdir()
    proc = _bare("etool", cwd=sibling, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "PREFIX-G"


def test_nested_registered_empty_project_falls_back_to_global(
    tmp_path, skills_root, csk_home
):
    parent = make_project(tmp_path, "parent")
    repo, _commit = _make_command_skill(
        skills_root, "skill-p", {"ntool": "PARENT-N"}, tag="v1"
    )
    _retitle_skill(repo, {"ntool": "GLOBAL-N"}, tag="v2")
    _install_project(csk_home, skills_root, parent, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, parent, [{"name": "skill-p", "tag": "v2"}])

    nested = _make_nested_project(parent / "nested")
    _install_project(csk_home, skills_root, nested, [])

    registry, _generation = _read_registry(csk_home)
    assert len(registry["projects"]) == 2
    nested_entry = _entry_for_root(registry, nested.resolve())
    assert nested_entry["commands"] == {}

    proc = _bare("ntool", cwd=nested, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["GLOBAL-N", f"ROOT:{nested.resolve()}"]


def test_nested_registered_empty_project_without_global_is_unavailable(
    tmp_path, skills_root, csk_home
):
    parent = make_project(tmp_path, "parent")
    _make_command_skill(skills_root, "skill-p", {"ntool": "PARENT-N"}, tag="v1")
    _install_project(csk_home, skills_root, parent, [{"name": "skill-p", "tag": "v1"}])
    nested = _make_nested_project(parent / "nested")
    _install_project(csk_home, skills_root, nested, [])

    proc = _bare("ntool", cwd=nested, csk_home=csk_home)
    assert proc.returncode == 127
    assert "unavailable" in proc.stderr
    assert str(nested.resolve()) in proc.stderr
    assert "PARENT-N" not in proc.stdout


def test_same_skill_replacement_suppresses_removed_global_export(
    tmp_path, skills_root, csk_home
):
    repo, _commit = _make_command_skill(
        skills_root,
        "skill-swap",
        {"keep": "KEEP-V1", "drop": "DROP-V1"},
        tag="v1",
    )
    _retitle_skill(repo, {"keep": "KEEP-V2"}, tag="v2")
    project = make_project(tmp_path, "proj-p")
    _install_global(
        csk_home, skills_root, project, [{"name": "skill-swap", "tag": "v1"}]
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-swap", "tag": "v2"}]
    )

    kept = _bare("keep", cwd=project, csk_home=csk_home)
    assert kept.returncode == 0, kept.stderr
    assert kept.stdout.splitlines()[0] == "KEEP-V2"

    dropped = _bare("drop", cwd=project, csk_home=csk_home)
    assert dropped.returncode == 127
    assert "unavailable" in dropped.stderr
    assert "suppress" in dropped.stderr
    assert "DROP-V1" not in dropped.stdout

    outside = tmp_path / "outside"
    outside.mkdir()
    leaked = _bare("drop", cwd=outside, csk_home=csk_home)
    assert leaked.returncode == 0, leaked.stderr
    assert leaked.stdout.splitlines()[0] == "DROP-V1"


def _install_global_allowing_errors(
    csk_home: Path,
    skills_root: Path,
    project: Path,
    declarations: list[dict[str, str]],
):
    root = csk_home / "global"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": ["claude_code"],
                "skills": declarations,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    return global_install.install(cfg)


def test_cross_layer_different_owners_refuse_at_publication(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-pa", {"shared": "PROJECT-SHARED"}, tag="v1")
    _make_command_skill(skills_root, "skill-ga", {"shared": "GLOBAL-SHARED"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-pa", "tag": "v1"}])

    result = _install_global_allowing_errors(
        csk_home, skills_root, project, [{"name": "skill-ga", "tag": "v1"}]
    )
    assert result.errors, "global install must refuse the cross-layer collision"
    assert "different" in result.errors[0]
    assert "skill-pa" in result.errors[0]
    assert "skill-ga" in result.errors[0]

    # The refused layer records nothing: the project pin keeps working.
    proc = _bare("shared", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "PROJECT-SHARED"


def test_cross_layer_different_owners_refuse_when_project_installs_second(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-pa", {"shared": "PROJECT-SHARED"}, tag="v1")
    _make_command_skill(skills_root, "skill-ga", {"shared": "GLOBAL-SHARED"}, tag="v1")
    _install_global(csk_home, skills_root, project, [{"name": "skill-ga", "tag": "v1"}])

    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "skill-pa", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    results = installer.install(cfg)
    assert len(results) == 1
    assert results[0].errors, "project install must refuse the cross-layer collision"
    assert "different" in results[0].errors[0]
    assert "skill-pa" in results[0].errors[0]
    assert "skill-ga" in results[0].errors[0]

    # The refused layer records nothing: the global pin keeps working.
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _bare("shared", cwd=outside, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "GLOBAL-SHARED"


def test_dispatch_refuses_inconsistent_cross_layer_record(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-pa", {"shared": "PROJECT-SHARED"}, tag="v1")
    _make_command_skill(skills_root, "skill-ga", {"gother": "GLOBAL-OTHER"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-pa", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-ga", "tag": "v1"}])

    # Publication refuses to record this inconsistency, so simulate a
    # stale record: the global layer gains the colliding name with a
    # valid digest copied from its own recorded target.
    registry, generation = _read_registry(csk_home)
    donor = registry["global"]["commands"]["gother"]
    registry["global"]["commands"]["shared"] = {
        "owner": "skill-ga",
        "target": donor["target"],
        "kind": donor["kind"],
        "digest": donor["digest"],
    }
    registry_path = dispatch.generations_dir(csk_home) / generation / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    proc = _bare("shared", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "different skills" in proc.stderr
    assert "skill-pa" in proc.stderr
    assert "skill-ga" in proc.stderr
    assert "PROJECT-SHARED" not in proc.stdout
    assert "GLOBAL-OTHER" not in proc.stdout

    # Outside the project there is no effective-scope collision.
    outside = tmp_path / "outside"
    outside.mkdir()
    proc_outside = _bare("shared", cwd=outside, csk_home=csk_home)
    assert proc_outside.returncode == 0, proc_outside.stderr
    assert proc_outside.stdout.splitlines()[0] == "GLOBAL-OTHER"


def test_two_skills_exporting_one_name_refuse_without_dispatch_record(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-x", {"clash": "X"}, tag="v1")
    _make_command_skill(skills_root, "skill-y", {"clash": "Y"}, tag="v1")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [
                {"name": "skill-x", "tag": "v1"},
                {"name": "skill-y", "tag": "v1"},
            ],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    results = installer.install(cfg)
    assert len(results) == 1
    assert results[0].errors
    assert "collision" in results[0].errors[0].lower()
    assert not (dispatch.dispatch_dir(csk_home) / "current").exists()


def test_reserved_command_name_refuses_without_dispatch_shim(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-r", {"git": "RESERVED"}, tag="v1")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "skill-r", "tag": "v1"}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    results = installer.install(cfg)
    assert len(results) == 1
    assert results[0].errors
    assert "reserved" in results[0].errors[0].lower()
    assert not (dispatch.shims_dir(csk_home) / "git").exists()


@pytest.mark.parametrize(
    "damage",
    ["truncated", "missing-generation", "bad-pointer", "empty-pointer"],
)
def test_failed_registry_read_is_error_never_absence(
    tmp_path, skills_root, csk_home, damage
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"dtool": "DTOOL"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    pointer = dispatch.dispatch_dir(csk_home) / "current"
    generation = pointer.read_text(encoding="utf-8").strip()
    registry_path = dispatch.generations_dir(csk_home) / generation / "registry.json"

    if damage == "truncated":
        registry_path.write_bytes(b'{"schema_version": 1,')
    elif damage == "missing-generation":
        shutil.rmtree(registry_path.parent)
    elif damage == "bad-pointer":
        pointer.write_text("does-not-exist\n", encoding="utf-8")
    elif damage == "empty-pointer":
        pointer.write_text("\n", encoding="utf-8")

    proc = _bare("dtool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "unavailable" not in proc.stderr
    assert "registry" in proc.stderr or "generation" in proc.stderr


def test_unreadable_pointer_and_generation_are_errors(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"dtool": "DTOOL"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    pointer = dispatch.dispatch_dir(csk_home) / "current"
    generation = pointer.read_text(encoding="utf-8").strip()
    registry_path = dispatch.generations_dir(csk_home) / generation / "registry.json"
    for target in (pointer, registry_path):
        target.chmod(0)
        try:
            proc = _bare("dtool", cwd=project, csk_home=csk_home)
        finally:
            target.chmod(0o600)
        assert proc.returncode == 1, (target, proc.returncode, proc.stderr)
        assert "unavailable" not in proc.stderr
        assert "registry" in proc.stderr or "generation" in proc.stderr


def test_deleted_cwd_is_error_never_outside_project(
    tmp_path, skills_root, csk_home, capsys
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"dtool": "DTOOL"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    dispatcher = dispatch.dispatcher_path(csk_home)

    victim = tmp_path / "gone"
    victim.mkdir()
    here = os.getcwd()
    os.chdir(victim)
    try:
        os.rmdir(victim)
        code = _dispatch_runtime.main([str(dispatcher), "dtool"])
    finally:
        os.chdir(here)
    assert code == 1
    captured = capsys.readouterr()
    assert "current directory" in captured.err


def test_dispatcher_ignores_hostile_pythonpath(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"dtool": "DTOOL"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])

    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "json.py").write_text('raise SystemExit("PWNED-json")\n', encoding="utf-8")
    (evil / "os.py").write_text('raise SystemExit("PWNED-os")\n', encoding="utf-8")
    (evil / "sitecustomize.py").write_text(
        'raise SystemExit("PWNED-site")\n', encoding="utf-8"
    )
    proc = _bare(
        "dtool", cwd=project, csk_home=csk_home, env_extra={"PYTHONPATH": str(evil)}
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "DTOOL"
    assert "PWNED" not in proc.stdout + proc.stderr


def _identity_entry(checkout_id: str, root: Path) -> dict:
    info = root.stat()
    return {
        "checkout_id": checkout_id,
        "canonical_root": str(root),
        "root_identity": {
            "st_ino": str(info.st_ino),
        },
    }


def test_find_project_matches_by_filesystem_identity(tmp_path):
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    evil = tmp_path / "proj-evil"
    (evil / "deep").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    lookalike = tmp_path / "proj-evil-x"
    lookalike.mkdir()
    projects = {
        "a" * 64: _identity_entry("a" * 64, proj),
        "b" * 64: _identity_entry("b" * 64, evil),
    }
    find = _dispatch_runtime.find_project
    assert find(projects, str(evil / "deep"))["canonical_root"] == str(evil)
    assert find(projects, str(proj / "sub"))["canonical_root"] == str(proj)
    assert find(projects, str(proj))["canonical_root"] == str(proj)
    assert find(projects, str(proj / "." / "sub"))["canonical_root"] == str(proj)
    assert find(projects, str(other)) is None
    assert find(projects, str(evil))["canonical_root"] == str(evil)
    assert find(projects, str(lookalike)) is None
    assert find({}, str(other)) is None


def test_contradictory_registrations_for_one_directory_refuse(tmp_path):
    # Publication dedupes by directory, so two live records for one
    # directory are hand corruption; the refusal is scoped to CWDs
    # beneath it and never fires elsewhere.
    root = tmp_path / "work"
    (root / "sub").mkdir(parents=True)
    projects = {
        "a" * 64: _identity_entry("a" * 64, root),
        "b" * 64: _identity_entry("b" * 64, root),
    }
    with pytest.raises(DispatchError, match="contradictory"):
        _dispatch_runtime.find_project(projects, str(root / "sub"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _dispatch_runtime.find_project(projects, str(elsewhere)) is None


def test_stale_deepest_record_refuses_instead_of_falling_through(tmp_path):
    parent = tmp_path / "parent"
    nested = parent / "nested"
    nested.mkdir(parents=True)
    projects = {
        "a" * 64: _identity_entry("a" * 64, parent),
        "b" * 64: {
            "checkout_id": "b" * 64,
            "canonical_root": str(nested),
            "root_identity": {"st_ino": "1"},
        },
    }
    assert projects["b" * 64]["root_identity"]["st_ino"] != str(
        nested.stat().st_ino
    )
    with pytest.raises(DispatchError, match="Re-register"):
        _dispatch_runtime.find_project(projects, str(nested))
    # The live parent still selects CWDs directly beneath it.
    assert (
        _dispatch_runtime.find_project(projects, str(parent))["checkout_id"]
        == "a" * 64
    )


def test_global_selective_run_preserves_retained_dispatch_records(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-keep", {"keep-a": "KEEP-A"}, tag="v1")
    repo, _commit = _make_command_skill(
        skills_root, "skill-bump", {"keep-b": "BUMP-V1"}, tag="v1"
    )
    _retitle_skill(repo, {"keep-b": "BUMP-V2"}, tag="v2")
    _install_global(
        csk_home,
        skills_root,
        project,
        [
            {"name": "skill-keep", "tag": "v1"},
            {"name": "skill-bump", "tag": "v1"},
        ],
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    before = _bare("keep-a", cwd=outside, csk_home=csk_home)
    assert before.stdout.splitlines()[0] == "KEEP-A"

    _install_global(
        csk_home,
        skills_root,
        project,
        [
            {"name": "skill-keep", "tag": "v1"},
            {"name": "skill-bump", "tag": "v2"},
        ],
        only=["skill-bump"],
    )

    retained = _bare("keep-a", cwd=outside, csk_home=csk_home)
    assert retained.returncode == 0, retained.stderr
    assert retained.stdout.splitlines()[0] == "KEEP-A"
    bumped = _bare("keep-b", cwd=outside, csk_home=csk_home)
    assert bumped.returncode == 0, bumped.stderr
    assert bumped.stdout.splitlines()[0] == "BUMP-V2"


def test_command_removed_everywhere_drops_shim_and_refuses(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-t", {"rtool": "RTOOL"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-t", "tag": "v1"}])
    shim = dispatch.shims_dir(csk_home) / "rtool"
    assert shim.exists()
    assert _bare("rtool", cwd=project, csk_home=csk_home).returncode == 0

    _install_project(csk_home, skills_root, project, [])
    assert not shim.exists()
    assert dispatch.dispatcher_path(csk_home).exists()
    proc = _bare("rtool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 127
    assert "not found" in proc.stderr


def test_shim_and_dispatcher_use_absolute_entry_points_only(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"dtool": "DTOOL"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])

    content = (dispatch.shims_dir(csk_home) / "dtool").read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert len(lines) == 2
    assert lines[1] == dispatch.shim_line(
        dispatch.dispatcher_path(csk_home), "dtool"
    )
    assert lines[1].startswith("exec ")
    assert str(dispatch.dispatcher_path(csk_home)) in lines[1]
    assert "dtool" in lines[1]
    assert '"$@"' in lines[1]
    for token in ("csk", "python", "python3"):
        assert f" {token} " not in f" {lines[1]} "

    wrapper = dispatch.dispatcher_path(csk_home).read_text(encoding="utf-8")
    wrapper_lines = wrapper.splitlines()
    assert wrapper_lines[0] == "#!/bin/sh"
    assert dispatch.sh_single_quote(sys.executable) in wrapper
    assert (
        dispatch.sh_single_quote(str(dispatch.dispatcher_runtime_path(csk_home)))
        in wrapper
    )
    assert 'exec "$_CSK_PYTHON" -I "$_CSK_RUNTIME" "$@"' in wrapper_lines
    assert "csk dispatch setup" in wrapper

    runtime_bytes = dispatch.dispatcher_runtime_path(csk_home).read_bytes()
    assert runtime_bytes == dispatch._runtime_template_bytes()
    assert b"from csk" not in runtime_bytes
    assert b"import csk" not in runtime_bytes


def test_dispatch_setup_cli_reports_and_installs_idempotently(
    tmp_path, csk_home
):
    config_path = csk_home / "config.json"
    env = os.environ.copy()
    env["CSK_CONFIG"] = str(config_path)

    def run_dispatch(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "csk", "dispatch", *args],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
        )

    shown = run_dispatch("setup")
    assert shown.returncode == 0, shown.stderr
    assert f"Dispatch shims: {dispatch.shims_dir(csk_home)}" in shown.stdout
    assert dispatch.SETUP_MARKER_BEGIN in shown.stdout
    assert "export PATH" in shown.stdout
    assert dispatch.dispatcher_path(csk_home).exists()

    rc = tmp_path / "bashrc"
    first = run_dispatch("setup", "--install", "--rc-path", str(rc), "--shell", "bash")
    assert first.returncode == 0, first.stderr
    assert "Installed dispatch PATH entry" in first.stdout
    assert rc.read_text(encoding="utf-8").count(dispatch.SETUP_MARKER_BEGIN) == 1

    second = run_dispatch("setup", "--install", "--rc-path", str(rc), "--shell", "bash")
    assert second.returncode == 0, second.stderr
    assert "already present" in second.stdout
    assert rc.read_text(encoding="utf-8").count(dispatch.SETUP_MARKER_BEGIN) == 1

    helper = run_dispatch("setup", "--help")
    assert helper.returncode == 0


def _model_probe(registry: dict, cwd: str, command: str) -> tuple:
    """Independent naive oracle for the dispatch selection rules.

    Written against the design text, not against ``_dispatch_runtime``:
    deepest root by component, project set then global fallback, same-skill
    suppression of removed exports.
    """

    def components(path: str) -> list[str]:
        return [
            part
            for part in os.path.normpath(path).split(os.sep)
            if part not in ("", ".")
        ]

    wanted = components(cwd)
    best = None
    best_depth = -1
    for entry in registry["projects"].values():
        parts = components(entry["canonical_root"])
        if len(parts) <= len(wanted) and wanted[: len(parts)] == parts:
            if len(parts) > best_depth:
                best = entry
                best_depth = len(parts)
    project_root = best["canonical_root"] if best is not None else None
    project_commands = best["commands"] if best is not None else {}
    global_commands = registry["global"]["commands"]
    if best is not None and command in project_commands:
        selected = project_commands[command]
        other = global_commands.get(command)
        if other is not None and other["owner"] != selected["owner"]:
            return ("refused",)
        return ("exec", selected["owner"], selected["target"], project_root)
    if command in global_commands:
        selected = global_commands[command]
        if best is not None and selected["owner"] in {
            skill["name"] for skill in best["skills"]
        }:
            return ("suppressed", selected["owner"])
        return ("exec", selected["owner"], selected["target"], project_root)
    return ("unavailable",)


def _marker_for_target(markers: dict[tuple[str, str, str], str], target: str) -> str:
    parts = target.split("/")
    runtime_index = parts.index("runtime")
    owner = parts[runtime_index + 1]
    commit = parts[runtime_index + 2]
    command = os.path.basename(target)
    return markers[(owner, commit, command)]


def test_property_generated_layouts_match_model(tmp_path, skills_root):
    rng = random.Random(20261004)
    alpha_repo, _ = _make_command_skill(
        skills_root, "skill-alpha", {"ptool": "PROP-ALPHA"}, tag="v1"
    )
    beta_repo, _ = _make_command_skill(
        skills_root, "skill-beta", {"ptool": "PROP-BETA1"}, tag="v1"
    )
    _retitle_skill(beta_repo, {"ptool": "PROP-BETA2"}, tag="v2")
    _make_command_skill(
        skills_root,
        "skill-gamma",
        {"g-one": "PROP-G1", "g-two": "PROP-G2"},
        tag="v1",
    )
    commits = {
        ("skill-alpha", "v1"): _tag_commit(alpha_repo, "v1"),
        ("skill-beta", "v1"): _tag_commit(beta_repo, "v1"),
        ("skill-beta", "v2"): _tag_commit(beta_repo, "v2"),
    }
    gamma_repo = skills_root / "skill-gamma"
    commits[("skill-gamma", "v1")] = _tag_commit(gamma_repo, "v1")
    markers = {
        ("skill-alpha", commits[("skill-alpha", "v1")], "ptool"): "PROP-ALPHA",
        ("skill-beta", commits[("skill-beta", "v1")], "ptool"): "PROP-BETA1",
        ("skill-beta", commits[("skill-beta", "v2")], "ptool"): "PROP-BETA2",
        ("skill-gamma", commits[("skill-gamma", "v1")], "g-one"): "PROP-G1",
        ("skill-gamma", commits[("skill-gamma", "v1")], "g-two"): "PROP-G2",
    }
    beta_tags = ["v1", "v2"]

    for trial in range(4):
        # One skill owns `ptool` per trial in every layer: different
        # skills sharing a cross-layer name refuse at publication, so a
        # layout mixing alpha and beta `ptool` across layers could never
        # install. Same-skill version skew across layers still varies.
        ptool_owner = rng.choice(["skill-alpha", "skill-beta"])

        def random_decls() -> list[dict[str, str]] | None:
            roll = rng.random()
            if roll < 0.15:
                return []
            if roll < 0.30:
                return None
            pool: list[dict[str, str]] = []
            if ptool_owner == "skill-alpha":
                pool.append({"name": "skill-alpha", "tag": "v1"})
            else:
                pool.append(
                    {"name": "skill-beta", "tag": rng.choice(beta_tags)}
                )
            if rng.random() < 0.5:
                pool.append({"name": "skill-gamma", "tag": "v1"})
            return pool
        home = tmp_path / f"prop-home-{trial}"
        home.mkdir()
        base = tmp_path / f"prop-{trial}"
        base.mkdir()
        project_dirs: list[Path] = []
        parent = base
        for index in range(rng.randint(1, 3)):
            if index > 0 and rng.random() < 0.5:
                parent = project_dirs[-1]
            candidate = parent / f"proj-{trial}-{index}"
            _make_nested_project(candidate)
            project_dirs.append(candidate)
        for project_dir in project_dirs:
            declarations = random_decls()
            if declarations is None:
                continue
            _install_project(home, skills_root, project_dir, declarations)
        global_decls = random_decls()
        if global_decls is not None:
            _install_global(home, skills_root, project_dirs[0], global_decls)
        registry, _generation = _read_registry(home)
        outside = base / "outside"
        outside.mkdir()
        for probe in range(6):
            roll = rng.random()
            if roll < 0.50:
                chosen = rng.choice(project_dirs)
                if rng.random() < 0.5:
                    sub = chosen / f"sub-{probe}"
                    sub.mkdir(exist_ok=True)
                    cwd = sub
                else:
                    cwd = chosen
            elif roll < 0.65:
                link = base / f"link-{trial}-{probe}"
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(
                    rng.choice(project_dirs), target_is_directory=True
                )
                cwd = link
            elif roll < 0.80:
                cwd = outside
            else:
                cwd = base
            command = rng.choice(["ptool", "g-one", "g-two", "nope-missing"])
            expected = _model_probe(registry, str(cwd.resolve()), command)
            proc = _bare(command, cwd=cwd, csk_home=home)
            context = (trial, probe, str(cwd), command, proc.returncode, proc.stderr)
            if expected[0] == "exec":
                _owner, target, project_root = expected[1], expected[2], expected[3]
                assert proc.returncode == 0, context
                root_line = project_root if project_root is not None else "unset"
                assert proc.stdout.splitlines() == [
                    _marker_for_target(markers, target),
                    f"ROOT:{root_line}",
                ], context
            elif expected[0] == "suppressed":
                assert proc.returncode == 127, context
                assert "suppress" in proc.stderr, context
            elif expected[0] == "refused":
                assert proc.returncode == 1, context
                assert "different skills" in proc.stderr, context
            else:
                assert proc.returncode == 127, context
                # The dispatcher reports "unavailable" when a shim exists
                # but the effective scope lacks the name; the shell
                # reports "not found" when no layer exports it at all.
                assert (
                    "unavailable" in proc.stderr or "not found" in proc.stderr
                ), context


def _is_within(path: str, root: Path) -> bool:
    """Return whether ``path`` resolves inside ``root`` (or is it)."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    want = os.path.realpath(root)
    return real == want or real.startswith(want + os.sep)


def test_project_add_publishes_empty_scope_boundary(
    tmp_path, skills_root, csk_home, monkeypatch
):
    parent = make_project(tmp_path, "parent")
    repo, _commit = _make_command_skill(
        skills_root, "skill-p", {"ntool": "PARENT-N"}, tag="v1"
    )
    _retitle_skill(repo, {"ntool": "GLOBAL-N"}, tag="v2")
    _install_project(csk_home, skills_root, parent, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, parent, [{"name": "skill-p", "tag": "v2"}])
    nested = make_project(parent, "nested")
    cfg = make_config(csk_home, skills_root, parent, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    assert cli.main(["project", "add", "nested", str(nested)]) == 0
    assert load_config().projects["nested"].path.samefile(nested)
    registry, _generation = _read_registry(csk_home)
    nested_entry = _entry_for_root(registry, nested.resolve())
    assert nested_entry["skills"] == []
    assert nested_entry["commands"] == {}

    # Reinstalling only the parent keeps the registered-never-installed
    # nested scope as a boundary: global fallback, never the parent set.
    results = installer.install(load_config(), alias="app")
    assert not results[0].errors, results[0].errors
    registry, _generation = _read_registry(csk_home)
    assert (
        _entry_for_root(registry, nested.resolve())["checkout_id"]
        in registry["projects"]
    )
    proc = _bare("ntool", cwd=nested, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["GLOBAL-N", f"ROOT:{nested.resolve()}"]


def test_registered_never_installed_nested_without_global_is_unavailable(
    tmp_path, skills_root, csk_home, monkeypatch
):
    parent = make_project(tmp_path, "parent")
    _make_command_skill(skills_root, "skill-p", {"ntool": "PARENT-N"}, tag="v1")
    _install_project(csk_home, skills_root, parent, [{"name": "skill-p", "tag": "v1"}])
    nested = make_project(parent, "nested")
    cfg = make_config(csk_home, skills_root, parent, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    assert cli.main(["project", "add", "nested", str(nested)]) == 0

    results = installer.install(load_config(), alias="app")
    assert not results[0].errors, results[0].errors
    proc = _bare("ntool", cwd=nested, csk_home=csk_home)
    assert proc.returncode == 127
    assert "unavailable" in proc.stderr
    assert str(nested.resolve()) in proc.stderr
    assert "PARENT-N" not in proc.stdout


def test_case_alias_selects_same_project_pin(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path, "PanelCase")
    alternate = project.with_name("panelcase")
    if not alternate.exists() or not alternate.samefile(project):
        pytest.skip("host volume is case-sensitive")
    repo, _commit = _make_command_skill(
        skills_root, "skill-case", {"casecmd": "PINNED"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, alternate, [{"name": "skill-case", "tag": "v1"}]
    )
    _retitle_skill(repo, {"casecmd": "GLOBAL"}, tag="v2")
    _install_global(
        csk_home, skills_root, project, [{"name": "skill-case", "tag": "v2"}]
    )
    for cwd in (project, alternate):
        proc = _bare("casecmd", cwd=cwd, csk_home=csk_home)
        assert proc.returncode == 0, proc
        assert proc.stdout.splitlines()[0] == "PINNED", proc


def test_replaced_root_refuses_with_reregister_guidance(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    repo, _commit = _make_command_skill(
        skills_root, "skill-p", {"ptool": "PROJECT-P"}, tag="v1"
    )
    _retitle_skill(repo, {"ptool": "GLOBAL-P"}, tag="v2")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v2"}])
    registry, _generation = _read_registry(csk_home)
    recorded = next(iter(registry["projects"].values()))["root_identity"]

    shutil.rmtree(project)
    project.mkdir()
    live = project.stat()
    proc = _bare("ptool", cwd=project, csk_home=csk_home)
    if str(live.st_ino) == recorded["st_ino"]:
        # Stated bound: the filesystem recycled the directory inode,
        # so the replacement is indistinguishable from the original
        # root and the pin still serves. A rename (next test) always
        # refuses because the spelled path no longer resolves.
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines()[0] == "PROJECT-P"
    else:
        assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
        assert "Re-register" in proc.stderr
        assert "PROJECT-P" not in proc.stdout
        assert "GLOBAL-P" not in proc.stdout


def test_renamed_root_refuses_with_reregister_guidance(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    repo, _commit = _make_command_skill(
        skills_root, "skill-p", {"ptool": "PROJECT-P"}, tag="v1"
    )
    _retitle_skill(repo, {"ptool": "GLOBAL-P"}, tag="v2")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v2"}])

    moved = tmp_path / "proj-moved"
    project.rename(moved)
    proc = _bare("ptool", cwd=moved, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "Re-register" in proc.stderr
    assert "PROJECT-P" not in proc.stdout
    assert "GLOBAL-P" not in proc.stdout


def test_dotdot_and_trailing_slash_match_project(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"ptool": "PROJECT-P"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    child = project / "child"
    child.mkdir()
    shim = str(dispatch.shims_dir(csk_home) / "ptool")
    for form in (str(project) + "/", str(child / "..")):
        proc = subprocess.run(
            [shim], cwd=form, capture_output=True, text=True
        )
        assert proc.returncode == 0, (form, proc)
        assert proc.stdout.splitlines()[0] == "PROJECT-P", (form, proc)


def _replace_preserving_identity_metadata(target: Path, recorded: dict) -> None:
    """Rewrite target bytes while preserving the per-call identity tuple."""
    content = target.read_bytes()
    evil = content.replace(b"PROJECT-C", b"PROJECT-X")
    assert evil != content and len(evil) == len(content)
    target.write_bytes(evil)
    info = target.stat()
    assert info.st_ino == int(recorded["st_ino"])
    assert info.st_size == int(recorded["size"])
    os.utime(target, ns=(info.st_atime_ns, int(recorded["mtime_ns"])))


def test_status_check_catches_metadata_preserving_replacement(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"ctool": "PROJECT-C"}, tag="v1")
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    assert cli.main(["status", "app", "--check"]) == 0

    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    target = Path(entry["commands"]["ctool"]["target"])
    _replace_preserving_identity_metadata(
        target, entry["commands"]["ctool"]["digest"]
    )

    # Documented per-call bound: the cheap tuple cannot see this
    # replacement, so the call still runs the tampered bytes.
    proc = _bare("ctool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "PROJECT-X"

    # The full sha256 verification catches it.
    assert cli.main(["status", "app", "--check"]) == 1
    captured = capsys.readouterr()
    assert "sha256" in captured.err
    assert "ctool" in captured.err


def test_global_status_check_catches_metadata_preserving_replacement(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-g", {"gtool": "PROJECT-C"}, tag="v1")
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    _install_global(csk_home, skills_root, project, [{"name": "skill-g", "tag": "v1"}])
    assert cli.main(["global", "status", "--check"]) == 0

    registry, _generation = _read_registry(csk_home)
    recorded = registry["global"]["commands"]["gtool"]["digest"]
    target = Path(registry["global"]["commands"]["gtool"]["target"])
    _replace_preserving_identity_metadata(target, recorded)

    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _bare("gtool", cwd=outside, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "PROJECT-X"

    assert cli.main(["global", "status", "--check"]) == 1
    captured = capsys.readouterr()
    assert "sha256" in captured.err
    assert "gtool" in captured.err


def test_install_refuses_manager_home_inside_checkout(tmp_path, skills_root):
    project = make_project(tmp_path, "checkout")
    home = project / "tracked-manager"
    locking.provision_new_manager_home(home)
    _make_command_skill(
        skills_root, "skill-home", {"homecmd": "CHECKOUT-TARGET"}, tag="v1"
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "skill-home", "tag": "v1"}],
        },
    )
    results = installer.install(
        make_config(home, skills_root, project, agents=["claude_code"])
    )
    assert len(results) == 1
    assert results[0].errors, "install must refuse a manager inside a checkout"
    assert "inside the registered checkout" in results[0].errors[0]
    assert not (dispatch.dispatch_dir(home) / "current").exists()


def test_project_add_refuses_manager_home_inside_checkout(
    tmp_path, skills_root, monkeypatch, capsys
):
    project = make_project(tmp_path, "checkout")
    home = project / "tracked-manager"
    home.mkdir(parents=True)
    cfg = make_config(home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    assert cli.main(["project", "add", "sneaky", str(project)]) == cli.EXIT_CONFIG
    captured = capsys.readouterr()
    assert "inside" in captured.err
    assert "manager home" in captured.err
    assert "sneaky" not in load_config().projects
    assert not (dispatch.dispatch_dir(home) / "current").exists()


def test_dispatch_refuses_target_inside_checkout(tmp_path, skills_root, csk_home):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-h", {"htool": "LEGIT-HTOOL"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-h", "tag": "v1"}])

    # Simulate a stale record where a registered checkout overlaps the
    # manager home: the recorded target is healthy and consistent, so
    # only the separation gate can refuse it.
    registry, generation = _read_registry(csk_home)
    ancestor = tmp_path.resolve()
    info = ancestor.stat()
    checkout_id = "c" * 64
    registry["projects"][checkout_id] = {
        "checkout_id": checkout_id,
        "canonical_root": str(ancestor),
        "project_alias": "ancestor",
        "checkout_alias": None,
        "root_identity": {
            "st_ino": str(info.st_ino),
        },
        "skills": [],
        "commands": {},
    }
    registry_path = dispatch.generations_dir(csk_home) / generation / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    proc = _bare("htool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "inside the registered checkout" in proc.stderr
    assert "LEGIT-HTOOL" not in proc.stdout
    assert "reinstall" in proc.stderr.lower()


@pytest.mark.parametrize(
    "path_form",
    [
        "space x",
        "quote'x",
        'dquote"x',
        "dollar$x",
        "$(touch MARKER)",
        "`touch MARKER`",
        "line\nx",
        "star*x",
        "semi;x",
        "pipe|x",
    ],
)
def test_setup_snippet_is_literal_and_idempotent(
    tmp_path, path_form, monkeypatch
):
    home = tmp_path / path_form
    home.mkdir(parents=True)
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    rc = tmp_path / "operator.rc"
    assert (
        cli.main(
            ["dispatch", "setup", "--shell", "sh", "--install", "--rc-path", str(rc)]
        )
        == 0
    )
    proc = subprocess.run(
        ["/bin/sh", "-c", '. "$1"; . "$1"; printf "%s" "$PATH"', "sh", str(rc)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    assert not (tmp_path / "MARKER").exists()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "/usr/bin:/bin:" + str(home / "shims")


_HOSTILE_SHELL_FRAGMENTS = [
    "$(touch MARKER)",
    "`touch MARKER`",
    "$(a)$(b)",
    "`a`",
    "'single'",
    '"double"',
    "a'b\"c",
    "space here",
    "tab\there",
    "line\nbreak",
    "star*glob",
    "quest?mark",
    "semi;colon",
    "pipe|amp&",
    "dollar$var",
    "${brace}",
    "back\\slash",
    "bang!hist",
    "hash#comment",
    "tilde~user",
    "paren(a)",
    "lt<gt>",
]


def _available_shells() -> list[str]:
    shells = ["sh"]
    for name in ("bash", "zsh"):
        found = shutil.which(name)
        if found is not None:
            shells.append(found)
    return shells


def test_generated_shell_text_is_literal_property(tmp_path):
    rng = random.Random(20261004)
    shells = _available_shells()
    assert shells, "no POSIX shell available"
    pairs = [
        (rng.choice(_HOSTILE_SHELL_FRAGMENTS), rng.choice(_HOSTILE_SHELL_FRAGMENTS))
        for _ in range(12)
    ]
    for index, (path_frag, name_frag) in enumerate(pairs):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        manager = case_dir / path_frag
        manager.mkdir()
        marker = case_dir / "MARKER"
        shims = manager / "shims"
        snippet = dispatch.setup_snippet(shims, shell="sh")
        rc = case_dir / "operator.rc"
        rc.write_text(snippet, encoding="utf-8")
        stub = manager / "stub-dispatcher"
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
        stub.chmod(0o755)
        line = dispatch.shim_line(stub, name_frag)
        for shell in shells:
            sourced = subprocess.run(
                [shell, "-c", '. "$1"; . "$1"; printf "%s" "$PATH"', shell, str(rc)],
                cwd=case_dir,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": "/usr/bin:/bin"},
            )
            assert not marker.exists(), (shell, path_frag, snippet)
            assert sourced.returncode == 0, (shell, path_frag, sourced.stderr)
            assert sourced.stdout == "/usr/bin:/bin:" + str(shims), (
                shell,
                path_frag,
                snippet,
            )
            launched = subprocess.run(
                [shell, "-c", line, shell, "extra arg", ""],
                cwd=case_dir,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": "/usr/bin:/bin"},
            )
            assert not marker.exists(), (shell, name_frag, line)
            assert launched.returncode == 0, (shell, name_frag, launched.stderr)
            assert launched.stdout.splitlines() == [name_frag, "extra arg", ""], (
                shell,
                name_frag,
                line,
            )


@pytest.mark.parametrize(
    "name",
    ["space x", "quote'x", 'dollar$x', "tick`x", "line\nx", "star*x"],
)
def test_invalid_command_names_refuse_at_publication(tmp_path, name):
    home = tmp_path / "manager"
    home.mkdir()
    target = home / "target"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(dispatch.DispatchPublishError):
        dispatch.publish_project(
            home,
            canonical_root=str(root.resolve()),
            project_alias="p",
            checkout_alias=None,
            skills=[("literal-skill", "a" * 40)],
            commands={name: ("literal-skill", str(target), "script")},
        )


def _install_tx_v1(csk_home: Path, skills_root: Path, project: Path):
    repo, _commit = _make_command_skill(
        skills_root, "tx-skill", {"oldcmd": "OLD"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "tx-skill", "tag": "v1"}]
    )
    _retitle_skill(repo, {"oldcmd": "NEW", "newcmd": "NEW-EXPORT"}, tag="v2")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "tx-skill", "tag": "v2"}],
        },
    )
    return make_config(csk_home, skills_root, project, agents=["claude_code"])


def test_interrupted_generation_write_keeps_old_surface(
    tmp_path, skills_root, csk_home, monkeypatch
):
    project = make_project(tmp_path, "project")
    cfg = _install_tx_v1(csk_home, skills_root, project)
    _registry, old_generation = _read_registry(csk_home)

    def boom(*args, **kwargs):
        raise OSError("simulated interruption writing generation")

    monkeypatch.setattr(dispatch, "_write_generation_file", boom)
    results = installer.install(cfg)
    assert results[0].errors
    assert "simulated interruption" in results[0].errors[0]

    registry, generation = _read_registry(csk_home)
    assert generation == old_generation
    entry = next(iter(registry["projects"].values()))
    assert sorted(entry["commands"]) == ["oldcmd"]
    old = _bare("oldcmd", cwd=project, csk_home=csk_home)
    assert old.returncode == 0, old.stderr
    assert old.stdout.startswith("OLD")
    new = _bare("newcmd", cwd=project, csk_home=csk_home)
    assert new.returncode == 127


def test_interrupted_staging_keeps_old_surface(
    tmp_path, skills_root, csk_home, monkeypatch
):
    project = make_project(tmp_path, "project")
    cfg = _install_tx_v1(csk_home, skills_root, project)
    _registry, old_generation = _read_registry(csk_home)

    real = dispatch._write_dispatch_shim

    def interrupted(target_dir, name, dispatcher):
        if name == "newcmd":
            raise OSError("simulated interruption staging new shim")
        return real(target_dir, name, dispatcher)

    monkeypatch.setattr(dispatch, "_write_dispatch_shim", interrupted)
    results = installer.install(cfg)
    assert results[0].errors
    assert "simulated interruption" in results[0].errors[0]

    registry, generation = _read_registry(csk_home)
    assert generation == old_generation
    entry = next(iter(registry["projects"].values()))
    assert sorted(entry["commands"]) == ["oldcmd"]
    old = _bare("oldcmd", cwd=project, csk_home=csk_home)
    assert old.returncode == 0, old.stderr
    assert old.stdout.startswith("OLD")
    new = _bare("newcmd", cwd=project, csk_home=csk_home)
    assert new.returncode == 127


def test_interrupted_global_staging_keeps_old_surface(
    tmp_path, skills_root, csk_home, monkeypatch
):
    project = make_project(tmp_path, "project")
    _make_command_skill(skills_root, "skill-p", {"projectcmd": "PROJECT"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _make_command_skill(
        skills_root, "global-one", {"aaold": "GLOBAL-OLD"}, tag="v1"
    )
    _make_command_skill(
        skills_root, "global-two", {"zznew": "GLOBAL-NEW"}, tag="v1"
    )
    _install_global(
        csk_home, skills_root, project, [{"name": "global-one", "tag": "v1"}]
    )
    _registry, old_generation = _read_registry(csk_home)

    real = dispatch._write_dispatch_shim

    def interrupted(target_dir, name, dispatcher):
        if name == "zznew":
            raise OSError("simulated global interruption")
        return real(target_dir, name, dispatcher)

    monkeypatch.setattr(dispatch, "_write_dispatch_shim", interrupted)
    result = _install_global_allowing_errors(
        csk_home,
        skills_root,
        project,
        [{"name": "global-one", "tag": "v1"}, {"name": "global-two", "tag": "v1"}],
    )
    assert result.errors
    assert "simulated global interruption" in result.errors[0]

    registry, generation = _read_registry(csk_home)
    assert generation == old_generation
    assert len(registry["projects"]) == 1
    assert "zznew" not in registry["global"]["commands"]
    outside = tmp_path / "outside"
    outside.mkdir()
    old = _bare("aaold", cwd=outside, csk_home=csk_home)
    assert old.returncode == 0, old.stderr
    assert old.stdout.splitlines()[0] == "GLOBAL-OLD"
    new = _bare("zznew", cwd=outside, csk_home=csk_home)
    assert new.returncode == 127
    kept = _bare("projectcmd", cwd=project, csk_home=csk_home)
    assert kept.returncode == 0, kept.stderr


def test_interrupted_pointer_swap_keeps_old_surface(
    tmp_path, skills_root, csk_home, monkeypatch
):
    project = make_project(tmp_path, "project")
    cfg = _install_tx_v1(csk_home, skills_root, project)
    _registry, old_generation = _read_registry(csk_home)

    def boom(*args, **kwargs):
        raise OSError("simulated interruption swapping pointer")

    monkeypatch.setattr(dispatch, "_swap_current_pointer", boom)
    results = installer.install(cfg)
    assert results[0].errors
    assert "simulated interruption" in results[0].errors[0]

    registry, generation = _read_registry(csk_home)
    assert generation == old_generation
    entry = next(iter(registry["projects"].values()))
    assert sorted(entry["commands"]) == ["oldcmd"]
    old = _bare("oldcmd", cwd=project, csk_home=csk_home)
    assert old.returncode == 0, old.stderr
    assert old.stdout.startswith("OLD")
    # The new shim was staged before the failed switch; the still-old
    # dispatcher reports its name unavailable. Either 127 shape is the
    # coherent old surface.
    new = _bare("newcmd", cwd=project, csk_home=csk_home)
    assert new.returncode == 127


def test_interrupted_prune_keeps_new_surface(
    tmp_path, skills_root, csk_home, monkeypatch
):
    project = make_project(tmp_path, "project")
    repo, _commit = _make_command_skill(
        skills_root, "tx-skill", {"oldcmd": "OLD", "dropcmd": "DROP"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "tx-skill", "tag": "v1"}]
    )
    _registry, old_generation = _read_registry(csk_home)
    _retitle_skill(repo, {"oldcmd": "NEW", "newcmd": "NEW-EXPORT"}, tag="v2")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "tx-skill", "tag": "v2"}],
        },
    )

    calls = 0
    real = dispatch.prune_launchers

    def interrupted(home, registry):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("simulated interruption pruning launchers")
        return real(home, registry)

    monkeypatch.setattr(dispatch, "prune_launchers", interrupted)
    results = installer.install(
        make_config(csk_home, skills_root, project, agents=["claude_code"])
    )
    assert results[0].errors
    assert "simulated interruption" in results[0].errors[0]

    registry, generation = _read_registry(csk_home)
    assert generation != old_generation
    entry = next(iter(registry["projects"].values()))
    assert sorted(entry["commands"]) == ["newcmd", "oldcmd"]
    old = _bare("oldcmd", cwd=project, csk_home=csk_home)
    assert old.returncode == 0, old.stderr
    assert old.stdout.startswith("NEW")
    new = _bare("newcmd", cwd=project, csk_home=csk_home)
    assert new.returncode == 0, new.stderr
    assert new.stdout.startswith("NEW-EXPORT")
    # The stale shim survives the failed prune, but the new dispatcher
    # reports its removed name unavailable: the coherent new surface.
    assert (dispatch.shims_dir(csk_home) / "dropcmd").exists()
    dropped = _bare("dropcmd", cwd=project, csk_home=csk_home)
    assert dropped.returncode == 127
    assert "unavailable" in dropped.stderr

    monkeypatch.undo()
    dispatch.recover_launchers(csk_home)
    assert not (dispatch.shims_dir(csk_home) / "dropcmd").exists()
    again = _bare("oldcmd", cwd=project, csk_home=csk_home)
    assert again.returncode == 0, again.stderr


def test_recovery_reconciles_launchers_against_current(
    tmp_path, skills_root, csk_home, monkeypatch
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"rcmd": "RCMD"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    shim = dispatch.shims_dir(csk_home) / "rcmd"
    shim.unlink()
    stale = dispatch.shims_dir(csk_home) / "stale-ghost"
    stale.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    stale.chmod(0o755)

    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    assert cli.main(["dispatch", "setup"]) == 0

    assert shim.exists()
    assert not stale.exists()
    proc = _bare("rcmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "RCMD"


def test_dispatcher_reads_nothing_under_checkout(
    tmp_path, skills_root, csk_home, monkeypatch
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-h", {"htool": "LEGIT-HTOOL"}, tag="v1")
    _make_command_skill(skills_root, "skill-g", {"gtool": "GLOBAL-G"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-h", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-g", "tag": "v1"}])

    checkout_shim = project / ".agents" / "bin" / "htool"
    assert checkout_shim.exists()
    if checkout_shim.is_symlink() or checkout_shim.is_file():
        checkout_shim.unlink()
    checkout_shim.write_text('#!/bin/sh\necho "HOSTILE-BIN"\n', encoding="utf-8")
    checkout_shim.chmod(0o755)
    (project / ".agents" / "env.sh").write_text(
        '#!/bin/sh\ntouch "SHOULD-NOT-RUN"\n', encoding="utf-8"
    )
    (project / "Skillfile.json").write_text("not JSON; hostile post-install edit")
    hostile = tmp_path / "hostile"
    (hostile / ".agents" / "bin").mkdir(parents=True)
    (hostile / "Skillfile.json").write_text("not JSON either")

    registry, _generation = _read_registry(csk_home)
    project_entry = next(iter(registry["projects"].values()))
    recorded_target = project_entry["commands"]["htool"]["target"]
    canonical = project_entry["canonical_root"]
    global_target = registry["global"]["commands"]["gtool"]["target"]

    calls: list[tuple] = []

    def fake_execve(path: str, argv: list[str], env: dict) -> None:
        calls.append((path, argv, env))
        raise _ExecveIntercepted

    monkeypatch.setattr(os, "execve", fake_execve)
    dispatcher = str(dispatch.dispatcher_path(csk_home))

    def run_main(argv: list[str], cwd: Path, tracer: _CheckoutReadTracer) -> None:
        here = os.getcwd()
        os.chdir(cwd)
        try:
            with tracer:
                with pytest.raises(_ExecveIntercepted):
                    _dispatch_runtime.main(argv)
        finally:
            os.chdir(here)

    project_tracer = _CheckoutReadTracer(project)
    run_main([dispatcher, "htool", "a b"], project, project_tracer)
    assert len(calls) == 1
    target, argv, env = calls.pop()
    assert target == recorded_target
    assert argv == [recorded_target, "a b"]
    assert env["CSK_PROJECT_ROOT"] == canonical

    hostile_tracer = _CheckoutReadTracer(hostile)
    run_main([dispatcher, "gtool"], hostile, hostile_tracer)
    assert len(calls) == 1
    target, argv, env = calls.pop()
    assert target == global_target
    assert argv == [global_target]
    assert "CSK_PROJECT_ROOT" not in env

    assert project_tracer.violations() == []
    assert hostile_tracer.violations() == []
    # Non-empty proof: the tracer observed the manager reads it had
    # to see, so an empty trace cannot attest absence.
    assert project_tracer.manager_reads(csk_home), (
        "tracer saw no manager reads; absence proves nothing"
    )
    assert not (project / "SHOULD-NOT-RUN").exists()


@pytest.mark.parametrize(
    "shape",
    [
        "open-relative",
        "open-bytes",
        "os-open-relative",
        "os-open-dir-fd",
        "open-int-fd",
        "listdir-relative",
        "scandir-bytes",
        "audit-backstop",
    ],
)
def test_r14_read_tracer_catches_evasive_checkout_reads(tmp_path, shape):
    """F8/D8: the tracer normalizes relative, bytes and fd-based paths.

    Every evasive read shape through ``open``/``os.open``/listings is
    flagged, including ``dir_fd``-relative and int-fd opens resolved
    at event time, plus a call that bypasses the wrappers (caught by
    the audit backstop). Production call site: the committed
    ``_CheckoutReadTracer`` proving the dispatch-path read invariant.
    """
    project = make_project(tmp_path, "proj-p")
    (project / "Skillfile.json").write_text("{}", encoding="utf-8")
    here = os.getcwd()
    os.chdir(project)
    try:
        tracer = _CheckoutReadTracer(project)
        with tracer:
            if shape == "open-relative":
                with open("Skillfile.json", encoding="utf-8") as handle:
                    handle.read()
            elif shape == "open-bytes":
                with open(b"Skillfile.json", "rb") as handle:
                    handle.read()
            elif shape == "os-open-relative":
                fd = os.open("Skillfile.json", os.O_RDONLY)
                os.close(fd)
            elif shape == "os-open-dir-fd":
                anchor = os.open(project, os.O_RDONLY)
                try:
                    fd = os.open("Skillfile.json", os.O_RDONLY, dir_fd=anchor)
                finally:
                    os.close(anchor)
                os.close(fd)
            elif shape == "open-int-fd":
                fd = os.open(project / "Skillfile.json", os.O_RDONLY)
                try:
                    with open(fd, closefd=False) as handle:
                        handle.read()
                finally:
                    os.close(fd)
            elif shape == "listdir-relative":
                os.listdir(".")
            elif shape == "scandir-bytes":
                with os.scandir(b".") as iterator:
                    list(iterator)
            elif shape == "audit-backstop":
                tracer._originals["open"]("Skillfile.json", encoding="utf-8").close()
            else:  # pragma: no cover - exhaustive parametrize
                raise AssertionError(f"unknown shape {shape}")
    finally:
        os.chdir(here)
    assert tracer.violations(), f"{shape} was not flagged"
    assert any(
        "Skillfile.json" in hit or hit == os.path.realpath(project)
        for hit in tracer.violations()
    ), (shape, tracer.violations())


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(-(-len(ordered) * fraction // 1)) - 1))
    return ordered[index]


def _write_step_summary(lines: list[str]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def test_dispatch_latency_warm_p95_under_target(tmp_path):
    """Resolve-cost gate plus reported end-to-end numbers (option a).

    Orchestrator decision 2026-10-04 on the latency stop-the-line: the
    HARD gate is the dispatcher's own resolve cost (registry load +
    selection + target validation, the exact pre-exec sequence from
    ``_dispatch_runtime.main``), p95 <= 50ms at 1/10/100 registered
    projects. The end-to-end shim->dispatcher->target p95 is MEASURED
    and REPORTED (print, step summary, warnings summary) but never
    asserted: its tail is sh+python spawn scheduling (~30ms typical,
    50-90ms on loaded macOS; direct-exec baseline ~3ms), not dispatch
    logic.
    """
    scales = (1, 10, 100)
    resolve_samples_per_scale = 200
    e2e_samples_per_scale = 60
    report: list[str] = []
    # The design targets dispatch overhead excluding target startup, so the
    # probe target is a native binary (copied `true`), not a script: its
    # exec cost is the floor any launcher pays.
    true_source = shutil.which("true")
    for scale in scales:
        home = tmp_path / f"lat-home-{scale}"
        home.mkdir()
        target_dir = home / "lat-targets"
        target_dir.mkdir()
        target = target_dir / "lat-target"
        if true_source is not None:
            shutil.copy(true_source, target)
            target.chmod(0o755)
            expect_output: str | None = ""
        else:  # pragma: no cover - every POSIX CI host ships `true`
            target.write_text('#!/bin/sh\necho "lat-ok"\n', encoding="utf-8")
            target.chmod(0o755)
            expect_output = "lat-ok"
        roots: list[Path] = []
        for index in range(scale):
            root = home / f"proj-{index}"
            root.mkdir()
            roots.append(root)
            dispatch.publish_project(
                home,
                canonical_root=str(root.resolve()),
                project_alias=f"lat-{index}",
                checkout_alias=None,
                skills=[(f"lat-skill-{index}", "d" * 40)],
                commands={
                    "latcmd": (
                        f"lat-skill-{index}",
                        str(target),
                        "script",
                    )
                },
            )
        shim = dispatch.shims_dir(home) / "latcmd"
        assert shim.exists()
        cwd = str(roots[-1].resolve())
        manager = str(home)

        # HARD GATE: the dispatcher resolve path, timed in-process so the
        # measurement excludes interpreter/sh spawn scheduling entirely.
        def resolve_once() -> str:
            registry = _dispatch_runtime.load_registry(manager)
            entry, project_root, from_project = (
                _dispatch_runtime.select_command(registry, cwd, "latcmd")
            )
            scope, reinstall = _dispatch_runtime.scope_guidance(
                project_root=project_root,
                from_project=from_project,
                owner=entry["owner"],
            )
            return _dispatch_runtime.require_usable_target(
                manager,
                entry["target"],
                command="latcmd",
                scope=scope,
                reinstall=reinstall,
                expected_digest=entry["digest"],
                projects=registry["projects"],
            )

        assert resolve_once() == str(target)  # warmup + wiring check
        resolve_samples: list[float] = []
        for _ in range(resolve_samples_per_scale):
            start = time.perf_counter()
            resolved = resolve_once()
            resolve_samples.append(time.perf_counter() - start)
            assert resolved == str(target)
        resolve_ordered = sorted(resolve_samples)
        resolve_p50 = resolve_ordered[len(resolve_ordered) // 2]
        resolve_p95 = _percentile(resolve_samples, 0.95)
        resolve_line = (
            f"dispatch resolve scale={scale} "
            f"n={resolve_samples_per_scale} "
            f"p50={resolve_p50 * 1000:.2f}ms "
            f"p95={resolve_p95 * 1000:.2f}ms "
            f"max={resolve_ordered[-1] * 1000:.2f}ms"
        )
        report.append(resolve_line)
        # Progress is reported before the gate so a failure still shows
        # the distribution it failed on.
        print(resolve_line, flush=True)
        _write_step_summary([resolve_line])
        assert resolve_p95 <= 0.050, (
            f"dispatch resolve p95 too slow at scale {scale}"
        )

        # REPORT ONLY: real end-to-end shim->dispatcher->target. No assert.
        # The cold sample is reported, not gated: on macOS the first exec
        # of a newly published dispatcher pays a one-time OS security scan
        # (~0.5s) that no dispatcher code can remove.
        env = _shims_env(home)
        cold_start = time.perf_counter()
        cold = subprocess.run(
            [str(shim)],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
        )
        cold_elapsed = time.perf_counter() - cold_start
        assert cold.returncode == 0, cold.stderr
        assert cold.stdout.strip() == expect_output, cold.stdout
        samples: list[float] = []
        for _ in range(e2e_samples_per_scale):
            start = time.perf_counter()
            proc = subprocess.run(
                [str(shim)],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
            )
            samples.append(time.perf_counter() - start)
            assert proc.returncode == 0, proc.stderr
        warm_p95 = _percentile(samples, 0.95)
        ordered = sorted(samples)
        warm_median = ordered[len(ordered) // 2]
        # Interleaved direct-exec baseline on the same box and load: the
        # delta (bare minus direct) is the dispatch layer's own share.
        direct: list[float] = []
        for _ in range(15):
            start = time.perf_counter()
            direct_proc = subprocess.run(
                [str(target)],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
            )
            direct.append(time.perf_counter() - start)
            assert direct_proc.returncode == 0, direct_proc.stderr
        direct_median = sorted(direct)[len(direct) // 2]
        e2e_line = (
            f"dispatch e2e scale={scale} "
            f"cold={cold_elapsed * 1000:.1f}ms "
            f"warm_p50={warm_median * 1000:.1f}ms "
            f"warm_p95={warm_p95 * 1000:.1f}ms "
            f"warm_max={ordered[-1] * 1000:.1f}ms "
            f"direct_p50={direct_median * 1000:.1f}ms"
        )
        report.append(e2e_line)
        print(e2e_line, flush=True)
        _write_step_summary([e2e_line])
    # CI runs pytest with capture under xdist, so prints vanish on a pass;
    # the warnings summary is the only log-visible channel without a
    # workflow change (outside this task's scope). One warning carries
    # every line so `gh run view --log` shows the full table.
    warnings.warn("dispatch latency report:\n" + "\n".join(report), stacklevel=1)


# ---------------------------------------------------------------------------
# Revision-3 named regressions: every rev-2 panel reproduction becomes a
# committed test driven through the production entry point. Names carry the
# panel finding id (full-a findings, full-b F1-F11, delta D4/D8, and the
# recording-review N1/N11/N15 narrowing gaps).
# ---------------------------------------------------------------------------


def test_r4_case_alias_registration_deduplicates_identity(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """Full-a finding 1: registering a case alias keeps the installed scope.

    Production call site: ``cli.main(["project", "add", ...])`` ->
    ``dispatch.publish_empty_scope`` -> ``resolve_checkout_id``.
    """
    project = make_project(tmp_path, "PanelCase")
    alias = project.with_name("pANELcASE")
    if not alias.exists() or not alias.samefile(project):
        pytest.skip("host volume is case-sensitive")
    _make_command_skill(skills_root, "skill-case", {"casecmd": "PROJECT"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-case", "tag": "v1"}])
    before, _generation = _read_registry(csk_home)
    assert len(before["projects"]) == 1

    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    assert cli.main(["project", "add", "casealias", str(alias)]) == 0

    after, _generation = _read_registry(csk_home)
    assert len(after["projects"]) == 1
    entry = next(iter(after["projects"].values()))
    assert entry["commands"]["casecmd"]["owner"] == "skill-case"
    proc = _bare("casecmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "PROJECT"


def test_r5_missing_current_after_publication_is_corrupt(
    tmp_path, skills_root, csk_home
):
    """Full-a finding 2: losing an established pointer is registry damage.

    Production call site: bare shim -> ``_dispatch_runtime.main`` ->
    ``load_registry`` (kind ``current_missing``), exit 1 with repair
    guidance, never exit 127 "outside every project".
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"r5cmd": "PROJECT"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    (dispatch.dispatch_dir(csk_home) / "current").unlink()

    proc = _bare("r5cmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "error:" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "outside every registered project" not in proc.stderr
    assert "recover" in proc.stderr or "reinstall" in proc.stderr


@pytest.mark.parametrize("damage", ["pointer-nonutf8", "registry-nonutf8"])
def test_r5_nonutf8_bytes_are_guided_errors(
    tmp_path, skills_root, csk_home, damage
):
    """Full-a finding 3: undecodable bytes refuse without a traceback.

    Production call site: bare shim -> ``_dispatch_runtime.main`` ->
    ``load_registry`` (kind ``registry_unreadable``).
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"r5cmd": "PROJECT"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    registry, generation = _read_registry(csk_home)
    pointer = dispatch.current_path(csk_home)
    registry_file = dispatch.generation_registry_path(csk_home, generation)
    if damage == "pointer-nonutf8":
        pointer.write_bytes(b"\xff\n")
    else:
        registry_file.write_bytes(b"\xff")

    proc = _bare("r5cmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "error:" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "not valid UTF-8" in proc.stderr
    assert "reinstall" in proc.stderr


def test_r9_whole_skill_replacement_precedes_owner_check(
    tmp_path, skills_root, csk_home
):
    """Full-a finding 4: replacement applies before owner uniqueness.

    Global skill-a/v1 exports r9cmd; project skill-a/v2 drops it and
    project skill-b exports r9cmd. The effective set has exactly one
    owner (skill-b), so the install succeeds and the pin runs.
    Production call site: ``installer.install`` ->
    ``dispatch.publish_project`` -> ``compose_layers``.
    """
    project = make_project(tmp_path, "proj-p")
    repo, _commit = _make_command_skill(
        skills_root, "skill-a", {"r9cmd": "GLOBAL-A"}, tag="v1"
    )
    _retitle_skill(repo, {"r9other": "PROJECT-A"}, tag="v2")
    _make_command_skill(skills_root, "skill-b", {"r9cmd": "PROJECT-B"}, tag="v1")
    _install_global(csk_home, skills_root, project, [{"name": "skill-a", "tag": "v1"}])
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [
                {"name": "skill-a", "tag": "v2"},
                {"name": "skill-b", "tag": "v1"},
            ],
        },
    )
    results = installer.install(
        make_config(csk_home, skills_root, project, agents=["claude_code"])
    )
    assert not results[0].errors, results[0].errors
    proc = _bare("r9cmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("PROJECT-B")


def test_r12_interrupted_dispatcher_refresh_preserves_old(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """Full-a finding 5: staging never unlinks the working dispatcher.

    The old dispatcher (from a preceding manager release, modeled by an
    appended comment) keeps serving the old generation when the new
    wrapper staging fails: no unlink happens before the replacement is
    staged. Production call site: ``installer.install`` ->
    ``dispatch._commit_if_changed`` -> ``atomic_write_bytes``.
    """
    project = make_project(tmp_path, "proj-p")
    cfg = _install_tx_v1(csk_home, skills_root, project)
    wrapper = dispatch.dispatcher_path(csk_home)
    wrapper.write_bytes(wrapper.read_bytes() + b"\n# previous release\n")
    before = _bare("oldcmd", cwd=project, csk_home=csk_home)
    assert before.returncode == 0 and before.stdout.startswith("OLD")

    real = dispatch.tempfile.mkstemp

    def fault(*args, **kwargs):
        if kwargs.get("prefix", "").startswith(".dispatcher."):
            raise OSError(errno.ENOSPC, "injected interruption staging dispatcher")
        return real(*args, **kwargs)

    monkeypatch.setattr(dispatch.tempfile, "mkstemp", fault)
    results = installer.install(cfg)
    assert results[0].errors
    assert "injected interruption" in results[0].errors[0]

    assert wrapper.exists()
    after = _bare("oldcmd", cwd=project, csk_home=csk_home)
    assert after.returncode == 0, after.stderr
    assert after.stdout.startswith("OLD")


def test_r11_sigpipe_disposition_matches_direct(tmp_path, skills_root, csk_home):
    """F1: a dispatched target dies on SIGPIPE exactly like a direct call.

    Production call site: bare shim -> ``_dispatch_runtime.main`` ->
    ``restore_exec_signals`` -> ``os.execve``.
    """
    project = make_project(tmp_path, "proj-p")
    commands = {
        "sigcmd": (
            "#!/bin/sh\n"
            "kill -PIPE $$\n"
            'echo "survived-SIGPIPE"\n'
        )
    }
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    name: {"type": "script", "unix_path": f"scripts/{name}"}
                    for name in commands
                },
            }
        )
    }
    for name, body in commands.items():
        files[f"scripts/{name}"] = body
    make_skill_repo(skills_root, "skill-sg", files, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-sg", "tag": "v1"}])
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    target = entry["commands"]["sigcmd"]["target"]

    direct = subprocess.run(
        [target], cwd=project, text=True, capture_output=True
    )
    via = _bare("sigcmd", cwd=project, csk_home=csk_home)
    assert (direct.returncode, direct.stdout) == (via.returncode, via.stdout)
    assert direct.returncode == -signal.SIGPIPE


def test_r4_renamed_project_reregistered_keeps_dispatch_working(
    tmp_path, skills_root, csk_home
):
    """F2 (rename): reinstalling at the new path repairs dispatch.

    The old record goes stale and is ignored; the new record selects
    the moved project, and outside every project the global pin runs.
    Production call site: ``installer.install`` ->
    ``dispatch.publish_project`` -> ``resolve_checkout_id``.
    """
    _make_command_skill(skills_root, "skill-rn", {"rncmd": "RN-OK"}, tag="v1")
    old = make_project(tmp_path, "oldname")
    _install_global(csk_home, skills_root, old, [{"name": "skill-rn", "tag": "v1"}])
    _install_project(csk_home, skills_root, old, [{"name": "skill-rn", "tag": "v1"}])
    new = tmp_path / "newname"
    old.rename(new)
    _install_project(csk_home, skills_root, new, [{"name": "skill-rn", "tag": "v1"}])

    moved = _bare("rncmd", cwd=new, csk_home=csk_home)
    assert moved.returncode == 0, moved.stderr
    assert moved.stdout.splitlines()[0] == "RN-OK"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    globaled = _bare("rncmd", cwd=outside, csk_home=csk_home)
    assert globaled.returncode == 0, globaled.stderr
    assert globaled.stdout.splitlines()[0] == "RN-OK"


def test_r4_deleted_record_does_not_block_global_install(
    tmp_path, skills_root, csk_home
):
    """F2 (deleted root): a dead record cannot veto global publication.

    Production call site: ``global_install.install`` ->
    ``dispatch.publish_global`` (stale records skipped with a warning).
    """
    _make_command_skill(skills_root, "skill-da", {"shared": "A"}, tag="v1")
    _make_command_skill(skills_root, "skill-db", {"shared": "B"}, tag="v1")
    gone = make_project(tmp_path, "gone")
    _install_project(csk_home, skills_root, gone, [{"name": "skill-da", "tag": "v1"}])
    shutil.rmtree(gone)
    keep = make_project(tmp_path, "keep")
    result = _install_global_allowing_errors(
        csk_home, skills_root, keep, [{"name": "skill-db", "tag": "v1"}]
    )
    assert not result.errors, result.errors
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _bare("shared", cwd=outside, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "B"


def test_r3_install_publishes_empty_scope_for_skipped_nested(
    tmp_path, skills_root, csk_home
):
    """F3: a configured nested project without a Skillfile is a boundary.

    ``installer.install`` publishes the empty scope for the skipped
    project, so the nested root falls back to global instead of
    inheriting the parent pin. Production call site:
    ``installer.install`` -> ``_publish_empty_dispatch_scope`` ->
    ``dispatch.publish_empty_scope``.
    """
    from csk.config import GlobalConfig, ProjectConfig

    _make_command_skill(skills_root, "skill-nn", {"nncmd": "PARENT-PIN"}, tag="v1")
    parent = make_project(tmp_path, "parent")
    nested = make_project(parent, "nested")
    (nested / "Skillfile.json").unlink(missing_ok=True)
    write_skillfile(
        parent,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "skill-nn", "tag": "v1"}],
        },
    )
    cfg = GlobalConfig(
        path=csk_home / "config.json",
        skills_root=skills_root,
        preferred_locale="ru",
        default_agents=["claude_code"],
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={
            "parent": ProjectConfig(
                alias="parent", path=parent, agents=["claude_code"]
            ),
            "nested": ProjectConfig(
                alias="nested", path=nested, agents=["claude_code"]
            ),
        },
    )
    results = installer.install(cfg)
    assert len(results) == 2
    assert not results[0].errors, results[0].errors
    assert not results[1].errors, results[1].errors
    registry, _generation = _read_registry(csk_home)
    nested_entry = _entry_for_root(registry, nested.resolve())
    assert nested_entry["commands"] == {}
    proc = _bare("nncmd", cwd=nested, csk_home=csk_home)
    assert "PARENT-PIN" not in proc.stdout
    assert proc.returncode == 127


def test_r4_remount_simulation_ignores_device_change(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """F4: a bare st_dev change never breaks root matching.

    The matcher reads inodes, never device numbers; this simulates the
    panel's native APFS remount (same root, same inode, new device) by
    wrapping ``os.stat`` in-process and driving the production
    ``main``. Production call site: ``_dispatch_runtime.main`` ->
    ``find_project``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-rv", {"rvcmd": "RV-OK"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-rv", "tag": "v1"}])
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    recorded_target = entry["commands"]["rvcmd"]["target"]

    calls: list[tuple] = []

    def fake_execve(path: str, argv: list[str], env: dict) -> None:
        calls.append((path, argv, env))
        raise _ExecveIntercepted

    real_stat = os.stat

    class _RemountedStat:
        def __init__(self, wrapped: os.stat_result) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> object:
            if name == "st_dev":
                return self._wrapped.st_dev + 7919
            return getattr(self._wrapped, name)

    def remounted_stat(path: object, *args: object, **kwargs: object) -> object:
        return _RemountedStat(real_stat(path, *args, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setattr(_dispatch_runtime.os, "stat", remounted_stat)
    dispatcher = str(dispatch.dispatcher_path(csk_home))
    here = os.getcwd()
    os.chdir(project)
    try:
        with pytest.raises(_ExecveIntercepted):
            _dispatch_runtime.main([dispatcher, "rvcmd"])
    finally:
        os.chdir(here)
    assert len(calls) == 1
    target, argv, _env = calls[0]
    assert target == recorded_target
    assert argv == [recorded_target]


def test_r9_case_alias_across_layers_refuses(tmp_path, skills_root, csk_home):
    """F5: names differing only by case refuse on insensitive volumes.

    A project ``Zed`` and a global ``zed`` from different skills would
    collide in the one shims directory, so publication refuses instead
    of letting one layer's code run under the other's name.
    Production call site: ``installer.install`` ->
    ``dispatch.publish_project`` -> ``compose_layers``.
    """
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    if not probe.with_name("cASEpROBE").exists():
        pytest.skip("host volume is case-sensitive")
    project = make_project(tmp_path, "proj-p")
    upper_files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {"Zed": {"type": "script", "unix_path": "scripts/Zed"}},
            }
        ),
        "scripts/Zed": "#!/bin/sh\necho PROJECT-A-Upper\n",
    }
    make_skill_repo(skills_root, "skill-ska", upper_files, tag="v1")
    lower_files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {"zed": {"type": "script", "unix_path": "scripts/zed"}},
            }
        ),
        "scripts/zed": "#!/bin/sh\necho GLOBAL-B-lower\n",
    }
    make_skill_repo(skills_root, "skill-skb", lower_files, tag="v1")
    _install_global(csk_home, skills_root, project, [{"name": "skill-skb", "tag": "v1"}])
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "skill-ska", "tag": "v1"}],
        },
    )
    results = installer.install(
        make_config(csk_home, skills_root, project, agents=["claude_code"])
    )
    assert results[0].errors, "cross-layer case alias must refuse publication"
    assert "case" in results[0].errors[0].lower()


def test_r5_publication_refuses_unreadable_registry(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """F6: publication never heals a corrupt registry from an older pin.

    After corruption, ``project add`` refuses loudly (exit nonzero,
    naming recovery) instead of exiting 0 with an empty stderr and
    silently reverting the pin. Production call site:
    ``cli.main(["project", "add", ...])`` ->
    ``dispatch._registry_for_publish``.
    """
    repo, _commit = _make_command_skill(
        skills_root, "skill-hv", {"hcmd": "V1"}, tag="v1"
    )
    _retitle_skill(repo, {"hcmd": "V2"}, tag="v2")
    project = make_project(tmp_path, "proj-p")
    other = make_project(tmp_path, "other")
    _install_project(csk_home, skills_root, project, [{"name": "skill-hv", "tag": "v1"}])
    _install_project(csk_home, skills_root, project, [{"name": "skill-hv", "tag": "v2"}])
    before = _bare("hcmd", cwd=project, csk_home=csk_home)
    assert before.stdout.startswith("V2")

    _registry, generation = _read_registry(csk_home)
    (
        dispatch.generations_dir(csk_home) / generation / "registry.json"
    ).write_text("{garbage")
    corrupt = _bare("hcmd", cwd=project, csk_home=csk_home)
    assert corrupt.returncode == 1

    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    code = cli.main(["project", "add", "zed", str(other)])
    captured = capsys.readouterr()
    assert code != 0
    assert "recover" in captured.err
    # The corrupt generation is untouched: still an error, never V1.
    after = _bare("hcmd", cwd=project, csk_home=csk_home)
    assert after.returncode == 1
    assert not after.stdout.startswith("V1")


def test_r5_recover_restores_last_readable_generation_explicitly(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """F6 (repair path): ``dispatch recover`` is the explicit loud heal.

    It swaps ``current`` to the newest readable generation, says exactly
    what it did on stdout, and exits 0; with nothing readable it refuses
    and names ``--reset``. Production call site:
    ``cli.main(["dispatch", "recover"])`` -> ``dispatch.recover_registry``.
    """
    repo, _commit = _make_command_skill(
        skills_root, "skill-hv", {"hcmd": "V1"}, tag="v1"
    )
    _retitle_skill(repo, {"hcmd": "V2"}, tag="v2")
    project = make_project(tmp_path, "proj-p")
    keeper = make_project(tmp_path, "keeper")
    _install_project(csk_home, skills_root, project, [{"name": "skill-hv", "tag": "v1"}])
    _install_project(csk_home, skills_root, keeper, [{"name": "skill-hv", "tag": "v1"}])
    _install_project(csk_home, skills_root, project, [{"name": "skill-hv", "tag": "v2"}])
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    assert cli.main(["dispatch", "recover"]) == 0
    assert "nothing to recover" in capsys.readouterr().out

    _registry, generation = _read_registry(csk_home)
    (
        dispatch.generations_dir(csk_home) / generation / "registry.json"
    ).write_text("{garbage")
    assert cli.main(["dispatch", "recover"]) == 0
    recovered = capsys.readouterr()
    assert "recovered dispatch generation" in recovered.out
    assert "Reinstall" in recovered.out
    # Explicit downgrade to the last readable pin, loudly announced.
    proc = _bare("hcmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("V1")

    for child in dispatch.generations_dir(csk_home).iterdir():
        (child / "registry.json").write_text("{garbage}")
    assert cli.main(["dispatch", "recover"]) != 0
    assert "--reset" in capsys.readouterr().err
    assert cli.main(["dispatch", "recover", "--reset"]) == 0
    reset = capsys.readouterr()
    assert "reset" in reset.out
    assert "Reinstall" in reset.out


def test_r10_interpreter_path_with_space_installs_and_runs(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """F7 (space path): the wrapper execs a quoted absolute interpreter.

    A csk running from a path with a space (macOS pipx application
    support, any spaced venv) installs and dispatches instead of
    failing publication after materialization. Production call site:
    ``installer.install`` -> ``dispatch.ensure_dispatcher_wrapper``.
    """
    spaced = tmp_path / "Application Support" / "pipx" / "venvs" / "csk" / "bin"
    spaced.mkdir(parents=True)
    link = spaced / "python"
    link.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(link))
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-sp", {"hello": "SPACE-OK"}, tag="v1")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "skill-sp", "tag": "v1"}],
        },
    )
    results = installer.install(
        make_config(csk_home, skills_root, project, agents=["claude_code"])
    )
    assert not results[0].errors, results[0].errors
    proc = _bare("hello", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "SPACE-OK"


def test_r10_missing_interpreter_gives_guidance(tmp_path, skills_root, csk_home):
    """F7 (missing interpreter): a vanished interpreter names the repair.

    Production call site: bare shim -> generated wrapper's ``-x`` guard.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-ip", {"ipcmd": "IP-OK"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-ip", "tag": "v1"}])
    wrapper = dispatch.dispatcher_path(csk_home)
    text = wrapper.read_text(encoding="utf-8")
    poisoned = text.replace(
        dispatch.sh_single_quote(sys.executable),
        dispatch.sh_single_quote("/nonexistent/old-python"),
        1,
    )
    assert poisoned != text
    wrapper.write_text(poisoned, encoding="utf-8")

    proc = _bare("ipcmd", cwd=project, csk_home=csk_home)
    assert proc.returncode != 0
    assert "IP-OK" not in proc.stdout
    assert "csk dispatch setup" in proc.stderr
    assert "reinstall" in proc.stderr.lower()


def test_r12_setup_acquires_manager_lock(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """F9 (white-box): ``dispatch setup`` runs under the manager lock.

    Production call site: ``cli.main(["dispatch", "setup"])`` ->
    ``GlobalLock`` -> ``dispatch.recover_launchers``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-f9", {"f9cmd": "F9-OK"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-f9", "tag": "v1"}])
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    acquired: list[Path] = []
    real_lock = cli.GlobalLock

    class _RecordingLock(real_lock):  # type: ignore[misc]
        def __enter__(self) -> "_RecordingLock":
            acquired.append(Path(self._configured_home))
            return super().__enter__()

    monkeypatch.setattr(cli, "GlobalLock", _RecordingLock)
    assert cli.main(["dispatch", "setup"]) == 0
    assert acquired, "setup must acquire the manager lock"
    assert acquired[0] == csk_home or acquired[0].samefile(csk_home)


def test_r12_concurrent_setup_waits_for_publication(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """F9 (behavioral): setup during publication waits, then converges.

    A ``dispatch setup`` racing an install blocks on the manager lock
    instead of pruning a staged next-generation shim, and the final
    surface serves both commands. Production call sites: the real
    ``dispatch setup`` CLI in a second thread against
    ``installer.install`` paused mid-publish.
    """
    project = make_project(tmp_path, "proj-p")
    cfg = _install_tx_v1(csk_home, skills_root, project)
    save_config(cfg)
    # Freeze the test HOME so the racing setup cannot touch the real one.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.setenv("HOME", str(home))

    staged = threading.Event()
    release = threading.Event()
    real_swap = dispatch._swap_current_pointer

    def paused_swap(home_arg: Path, generation_id: str) -> None:
        staged.set()
        assert release.wait(timeout=60), "install was never released"
        real_swap(home_arg, generation_id)

    dispatch._swap_current_pointer = paused_swap
    install_outcome: list[object] = []

    def run_install() -> None:
        try:
            install_outcome.append(installer.install(cfg))
        except BaseException as exc:  # pragma: no cover - test plumbing
            install_outcome.append(exc)

    worker = threading.Thread(target=run_install, daemon=True)
    worker.start()
    try:
        assert staged.wait(timeout=60), "install never reached the swap"
        setup_outcome: list[int] = []

        def run_setup() -> None:
            setup_outcome.append(cli.main(["dispatch", "setup"]))

        setup_worker = threading.Thread(target=run_setup, daemon=True)
        setup_worker.start()
        # Setup must still be waiting on the publication lock.
        setup_worker.join(timeout=5)
        assert setup_worker.is_alive(), (
            "setup raced past the publication lock and could prune "
            "a staged shim"
        )
        release.set()
        worker.join(timeout=120)
        setup_worker.join(timeout=120)
        assert not setup_worker.is_alive()
        assert setup_outcome == [0]
    finally:
        dispatch._swap_current_pointer = real_swap
        release.set()

    assert len(install_outcome) == 1
    assert not isinstance(install_outcome[0], BaseException), install_outcome[0]
    assert not install_outcome[0][0].errors, install_outcome[0][0].errors
    for name, marker in (("oldcmd", "NEW"), ("newcmd", "NEW-EXPORT")):
        proc = _bare(name, cwd=project, csk_home=csk_home)
        assert proc.returncode == 0, (name, proc.stderr)
        assert proc.stdout.startswith(marker)


def test_r10_setup_preserves_rc_symlink_and_mode(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """F10: setup edits a symlinked rc target, keeping link and mode.

    Production call site: ``cli.main(["dispatch", "setup", "--install",
    ...])`` -> ``dispatch.install_setup_snippet``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-f10", {"f10cmd": "F10-OK"}, tag="v1")
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-f10", "tag": "v1"}]
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "zshrc"
    target.write_text("# operator rc\n", encoding="utf-8")
    target.chmod(0o644)
    link = tmp_path / ".zshrc"
    link.symlink_to(target)

    assert (
        cli.main(
            [
                "dispatch",
                "setup",
                "--install",
                "--rc-path",
                str(link),
                "--shell",
                "zsh",
            ]
        )
        == 0
    )
    assert link.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert dispatch.SETUP_MARKER_BEGIN in target.read_text(encoding="utf-8")


def test_r11_env_parity_matches_direct(tmp_path, skills_root, csk_home):
    """F11: the target sees the caller environment plus only the root.

    Across scrubbed, POSIX-C and UTF-8 locale bases, the dispatched
    target observes exactly the direct target's environment plus
    ``CSK_PROJECT_ROOT``: no ``LC_CTYPE`` coercion leak. Production
    call site: bare shim -> wrapper sentinels ->
    ``_dispatch_runtime.restore_caller_env`` -> ``os.execve``.
    """
    project = make_project(tmp_path, "proj-p")
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "dumpenv": {
                        "type": "script",
                        "unix_path": "scripts/dumpenv",
                    }
                },
            }
        ),
        "scripts/dumpenv": "#!/bin/sh\nenv | sort\n",
    }
    make_skill_repo(skills_root, "skill-ev", files, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-ev", "tag": "v1"}])
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    target = entry["commands"]["dumpenv"]["target"]

    bases = [
        {"PATH": os.environ["PATH"]},
        {"PATH": os.environ["PATH"], "LANG": "C"},
        {"PATH": os.environ["PATH"], "LANG": "en_US.UTF-8"},
        {
            "PATH": os.environ["PATH"],
            "LC_CTYPE": "C.UTF-8",
            "STICKY": "caller-value",
        },
    ]
    for base in bases:
        via_env = dict(base)
        via_env["PATH"] += os.pathsep + str(dispatch.shims_dir(csk_home))
        direct = subprocess.run(
            [target], cwd=project, env=dict(base), text=True, capture_output=True
        )
        via = subprocess.run(
            ["sh", "-c", 'exec "$@"', "sh", "dumpenv"],
            cwd=project,
            env=via_env,
            text=True,
            capture_output=True,
        )
        assert direct.returncode == 0, base
        assert via.returncode == 0, (base, via.stderr)
        direct_vars = dict(
            line.split("=", 1) for line in direct.stdout.splitlines() if "=" in line
        )
        via_vars = dict(
            line.split("=", 1) for line in via.stdout.splitlines() if "=" in line
        )
        # PATH legitimately differs (setup appends shims by design) and
        # "_" is a launcher artifact; everything else must match exactly,
        # including SHLVL and any caller-set locale variables.
        via_vars.pop("CSK_PROJECT_ROOT", None)
        via_vars.pop("PATH", None)
        direct_vars.pop("PATH", None)
        via_vars.pop("_", None)
        direct_vars.pop("_", None)
        assert via_vars == direct_vars, base


def test_d4_replaced_case_alias_root_refuses(tmp_path, skills_root, csk_home):
    """D4: a replaced root under a case alias refuses, never substitutes.

    The record was published via the lower-case spelling; the directory
    is renamed away (keeping its inode) and recreated under the
    upper-case spelling with a fresh identity. Dispatch from the
    recreated root must refuse with re-register guidance, not run the
    healthy global fallback. Production call site: bare shim ->
    ``_dispatch_runtime.main`` -> ``find_project`` (stale deepest).
    """
    project = make_project(tmp_path, "panelcase")
    upper = project.with_name("PanelCase")
    if not upper.exists() or not upper.samefile(project):
        pytest.skip("host volume is case-sensitive")
    repo, _commit = _make_command_skill(
        skills_root, "skill-case", {"casetool": "PROJECT"}, tag="v1"
    )
    _retitle_skill(repo, {"casetool": "GLOBAL"}, tag="v2")
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-case", "tag": "v1"}]
    )
    _install_global(
        csk_home, skills_root, project, [{"name": "skill-case", "tag": "v2"}]
    )
    registry, _generation = _read_registry(csk_home)
    recorded_ino = next(iter(registry["projects"].values()))["root_identity"][
        "st_ino"
    ]
    assert str(project.stat().st_ino) == recorded_ino

    retained = tmp_path / "retained-original"
    project.rename(retained)
    assert str(retained.stat().st_ino) == recorded_ino
    upper.mkdir()
    assert str(upper.stat().st_ino) != recorded_ino

    proc = _bare("casetool", cwd=upper, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "Re-register" in proc.stderr
    assert "GLOBAL" not in proc.stdout
    assert "PROJECT" not in proc.stdout
    assert "ROOT:unset" not in proc.stdout


def test_n1_inode_change_with_same_size_mtime_refuses(
    tmp_path, skills_root, csk_home
):
    """N1: same size and mtime but a new inode refuses the pin.

    The per-call tuple binds the activation inode: replacing the file
    (new inode) while preserving size and mtime is still detected, and
    the global fallback is never substituted. Production call site:
    bare shim -> ``require_usable_target``.
    """
    project = make_project(tmp_path, "proj-p")
    repo, _commit = _make_command_skill(
        skills_root, "skill-n1", {"n1tool": "PROJECT-N1"}, tag="v1"
    )
    _retitle_skill(repo, {"n1tool": "GLOBAL-N1"}, tag="v2")
    _install_project(csk_home, skills_root, project, [{"name": "skill-n1", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-n1", "tag": "v2"}])
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    recorded = entry["commands"]["n1tool"]["digest"]
    target = Path(entry["commands"]["n1tool"]["target"])

    replacement = tmp_path / "replacement"
    replacement.write_bytes(target.read_bytes())
    info = replacement.stat()
    assert info.st_ino != int(recorded["st_ino"])
    os.utime(
        replacement, ns=(info.st_atime_ns, int(recorded["mtime_ns"]))
    )
    replacement.chmod(0o755)
    replacement.replace(target)

    proc = _bare("n1tool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "GLOBAL-N1" not in proc.stdout
    assert "PROJECT-N1" not in proc.stdout
    assert "pinned by the project" in proc.stderr


def test_n11_unknown_schema_version_refuses(tmp_path, skills_root, csk_home):
    """N11: an unknown registry version is a guided error, not a guess.

    Production call site: bare shim -> ``_dispatch_runtime.main`` ->
    ``load_registry`` (kind ``unknown_version``).
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-sv", {"svcmd": "SV-OK"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-sv", "tag": "v1"}])
    registry, generation = _read_registry(csk_home)
    registry["schema_version"] = 2
    (
        dispatch.generations_dir(csk_home) / generation / "registry.json"
    ).write_text(json.dumps(registry), encoding="utf-8")

    proc = _bare("svcmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "schema_version" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "SV-OK" not in proc.stdout


def test_n15_global_upgrade_interrupted_keeps_old_pin(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """N15: interrupting a changed-pin global upgrade keeps the old pin.

    The global install moves to a new commit, then publication is
    interrupted after materialization: the still-active generation's
    bytes were retained through planning, so the old pin keeps working.
    Production call site: ``global_install.install`` -> dispatch
    retention + ``publish_global``.
    """
    project = make_project(tmp_path, "proj-p")
    repo, _commit = _make_command_skill(
        skills_root, "skill-gold", {"gold": "GOLD-V1"}, tag="v1"
    )
    _retitle_skill(repo, {"gold": "GOLD-V2"}, tag="v2")
    _install_global(csk_home, skills_root, project, [{"name": "skill-gold", "tag": "v1"}])
    outside = tmp_path / "outside"
    outside.mkdir()
    _registry, old_generation = _read_registry(csk_home)

    def boom(*args: object, **kwargs: object) -> object:
        raise OSError("simulated interruption swapping pointer")

    monkeypatch.setattr(dispatch, "_swap_current_pointer", boom)
    result = _install_global_allowing_errors(
        csk_home, skills_root, project, [{"name": "skill-gold", "tag": "v2"}]
    )
    assert result.errors
    assert "simulated interruption" in result.errors[0]

    _registry, generation = _read_registry(csk_home)
    assert generation == old_generation
    proc = _bare("gold", cwd=outside, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "GOLD-V1"


def test_ensure_dispatcher_refuses_csk_importing_template(
    tmp_path, monkeypatch
):
    """The runtime drift gate refuses a template importing the manager.

    Behavioral driver for the source-text gate: the installed
    dispatcher must stay stdlib-only, so a template containing a
    manager import refuses publication. Production call site:
    ``dispatch.ensure_dispatcher_runtime``.
    """
    home = tmp_path / "manager"
    home.mkdir()
    poisoned = (
        b'"""poisoned."""\nimport csk.dispatch\nSCHEMA_VERSION = 1\n'
    )
    monkeypatch.setattr(
        dispatch, "_runtime_template_bytes", lambda: poisoned
    )
    with pytest.raises(dispatch.DispatchPublishError, match="stdlib-only"):
        dispatch.ensure_dispatcher_runtime(home)
    assert not dispatch.dispatcher_runtime_path(home).exists()


# ---------------------------------------------------------------------------
# Revision-3 generated proof: one randomized model per invariant, each
# checked against an independently written reference.
# ---------------------------------------------------------------------------


def _reference_identity_match(records, cwd_parts, ino_of, recorded):
    """Naive oracle for root matching, written from the invariant text.

    ``records`` maps id -> root parts list; ``ino_of`` maps an absolute
    path string to its live inode (missing when the path is gone);
    ``recorded`` maps id -> recorded inode. Returns ("match", id) |
    ("stale", id) | ("moved", id) | ("contradictory",) | ("outside",).
    """

    def prefix_equal(record_parts, wanted):
        if len(record_parts) > len(wanted):
            return False
        for left, right in zip(record_parts, wanted):
            if left != right and left.casefold() != right.casefold():
                return False
        return True

    # Deepest path-prefix record decides.
    best_depth = -1
    candidates = []
    for checkout_id in sorted(records):
        parts = records[checkout_id]
        if not prefix_equal(parts, cwd_parts):
            continue
        if len(parts) > best_depth:
            best_depth = len(parts)
            candidates = [checkout_id]
        elif len(parts) == best_depth:
            candidates.append(checkout_id)
    if candidates:
        ancestor = "/" + "/".join(cwd_parts[:best_depth]) if best_depth else "/"
        live = [
            checkout_id
            for checkout_id in candidates
            if ino_of.get(ancestor) == recorded[checkout_id]
        ]
        if len(live) == 1:
            return ("match", live[0])
        if len(live) > 1:
            return ("contradictory",)
        return ("stale", candidates[0])
    chain_inos = set()
    for depth in range(len(cwd_parts) + 1):
        ancestor = "/" + "/".join(cwd_parts[:depth]) if depth else "/"
        if ancestor in ino_of:
            chain_inos.add(ino_of[ancestor])
    for checkout_id in sorted(records):
        if recorded[checkout_id] not in chain_inos:
            continue
        root = "/" + "/".join(records[checkout_id])
        if ino_of.get(root) == recorded[checkout_id]:
            return ("match", checkout_id)
        return ("moved", checkout_id)
    return ("outside",)


def test_identity_generated_trees_match_reference(tmp_path, monkeypatch):
    """Invariant 1: random trees agree with the reference model.

    Symlinks, case variants, moved and replaced roots, lookalike
    prefixes and remount simulation (device numbers rewritten under
    the matcher): every scenario matches the naive oracle, with and
    without the device rewrite. Production call site:
    ``_dispatch_runtime.find_project``.
    """
    rng = random.Random(20261004)
    names = ["proj", "proj-evil", "PROJ", "proj2", "a", "nest", "NEST-x"]
    real_stat = os.stat
    seen: set[str] = set()

    def dev_shifting_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        shift = int(hashlib.sha256(os.fspath(path).encode()).hexdigest()[:8], 16)

        class _Shifted:
            def __getattr__(self, name, _info=info, _shift=shift):
                if name == "st_dev":
                    return _info.st_dev + (_shift % 65521) + 1
                return getattr(_info, name)

        return _Shifted()

    for trial in range(150):
        case = tmp_path / f"ident-{trial}"
        case.mkdir()
        case = case.resolve()
        # Random tree with nesting, lookalikes and symlinks.
        existing: list[Path] = [case]
        for index in range(rng.randint(1, 5)):
            parent = rng.choice(existing)
            try:
                child = parent / f"{rng.choice(names)}-{index}"
                child.mkdir()
            except FileExistsError:
                continue
            existing.append(child)
            if rng.random() < 0.35 and len(existing) > 2:
                link = parent / f"link-{index}"
                try:
                    link.symlink_to(rng.choice(existing), target_is_directory=True)
                except (FileExistsError, OSError):
                    pass
        # Random records over the tree.
        records: dict[str, dict] = {}
        recorded: dict[str, int] = {}
        record_paths: dict[str, list[str]] = {}
        chosen = rng.sample(existing, k=min(len(existing), rng.randint(0, 3)))
        for number, root in enumerate(chosen):
            checkout_id = f"{trial:04d}{number:060d}"
            ino = root.stat().st_ino
            records[checkout_id] = {
                "checkout_id": checkout_id,
                "canonical_root": str(root),
                "root_identity": {"st_ino": str(ino)},
            }
            recorded[checkout_id] = ino
            record_paths[checkout_id] = [
                part for part in str(root).split("/") if part
            ]
        # Rarely, a duplicated registration of one directory (the
        # hand-corruption shape publication dedupes away).
        if records and rng.random() < 0.06:
            donor = rng.choice(sorted(records))
            twin = f"{trial:04d}9{'7' * 59}"
            records[twin] = dict(records[donor])
            records[twin]["checkout_id"] = twin
            recorded[twin] = recorded[donor]
            record_paths[twin] = record_paths[donor]
        # Mutations: rename, replace, or delete recorded roots.
        for checkout_id in list(records):
            roll = rng.random()
            root = Path(records[checkout_id]["canonical_root"])
            if roll < 0.15 and root.exists() and root != case:
                root.rename(case / f"moved-{checkout_id[-6:]}")
            elif roll < 0.25 and root.exists() and root != case:
                shutil.rmtree(root)
                root.mkdir()
            elif roll < 0.32 and root.exists() and root != case:
                shutil.rmtree(root)
        # Live inode map over physical paths.
        ino_of: dict[str, int] = {}
        for parent, dirs, _files in os.walk(case, followlinks=False):
            try:
                ino_of[parent] = real_stat(parent).st_ino
            except OSError:
                continue
            for name in list(dirs):
                full = os.path.join(parent, name)
                if os.path.islink(full):
                    dirs.remove(name)
        # Random CWD, sometimes spelled through a symlink.
        candidates = [path for path in ino_of if os.path.isdir(path)]
        if not candidates:
            continue
        raw_cwd = rng.choice(candidates)
        if rng.random() < 0.3:
            for parent, dirs, _files in os.walk(case, followlinks=False):
                for name in dirs:
                    link = os.path.join(parent, name)
                    if os.path.islink(link):
                        try:
                            target = os.path.realpath(link)
                        except OSError:
                            continue
                        if target in ino_of and rng.random() < 0.5:
                            raw_cwd = link
        physical_cwd = os.path.realpath(raw_cwd)
        cwd_parts = [part for part in physical_cwd.split("/") if part]
        expected = _reference_identity_match(
            record_paths, cwd_parts, ino_of, recorded
        )
        for wrap_stat in (False, True):
            if wrap_stat:
                monkeypatch.setattr(
                    _dispatch_runtime.os, "stat", dev_shifting_stat
                )
            else:
                monkeypatch.setattr(_dispatch_runtime.os, "stat", real_stat)
            try:
                found = _dispatch_runtime.find_project(records, raw_cwd)
            except DispatchError as exc:
                observed: tuple = (exc.kind, exc.kind)
                if exc.kind == "stale_root":
                    observed = ("stale", "")
                elif exc.kind == "moved_root":
                    observed = ("moved", "")
                elif exc.kind == "contradictory":
                    observed = ("contradictory",)
                else:
                    raise AssertionError(
                        f"trial {trial}: unexpected kind {exc.kind}"
                    ) from exc
            else:
                if found is None:
                    observed = ("outside",)
                else:
                    observed = ("match", found["checkout_id"])
            context = (trial, raw_cwd, expected, observed, wrap_stat)
            assert observed[0] == expected[0], context
            if expected[0] == "match":
                assert observed[1] == expected[1], context
            seen.add(expected[0])
    assert {"match", "stale", "moved", "outside"} <= seen, seen


def _reference_compose(project_skills, project_cmds, global_cmds, case_insensitive):
    """Naive oracle for layer composition, written from the invariant text.

    Returns ("ok", effective, suppressed) or ("error", kind), where
    ``effective`` maps each name to (owner, from_project).
    """
    skill_names = {skill["name"] for skill in project_skills}
    survivors = {
        name: owner
        for name, owner in global_cmds.items()
        if owner not in skill_names
    }
    suppressed = {
        name: owner
        for name, owner in global_cmds.items()
        if owner in skill_names
    }
    members = [(name, owner, True) for name, owner in project_cmds.items()]
    members.extend((name, owner, False) for name, owner in survivors.items())
    groups: dict[str, list] = {}
    for name, owner, from_project in members:
        key = name.casefold() if case_insensitive else name
        groups.setdefault(key, []).append((name, owner, from_project))
    for key in sorted(groups):
        group = groups[key]
        spellings = sorted({name for name, _o, _f in group})
        if len(spellings) > 1:
            return ("error", "case_collision")
        owners = sorted({owner for _n, owner, _f in group})
        if len(owners) > 1:
            return ("error", "owner_collision")
    effective = {}
    for key in sorted(groups):
        group = groups[key]
        name = group[0][0]
        project_entries = [owner for _n, owner, flag in group if flag]
        if project_entries:
            effective[name] = (project_entries[0], True)
        else:
            effective[name] = (group[0][1], False)
    return ("ok", effective, suppressed)


def test_compose_generated_layers_match_reference():
    """Invariant 4: random layer configurations agree with the reference.

    Replacement-before-owners plus the volume's case rules, differentially
    tested over hundreds of generated skill/command layouts, including
    same-skill skew, cross-layer owner clashes and case aliases.
    Production call site: ``_dispatch_runtime.compose_layers`` (shared
    by publication and dispatch).
    """
    rng = random.Random(20261005)
    skill_pool = ["skill-a", "skill-b", "skill-c", "skill-d"]
    command_pool = [
        "tool",
        "other",
        "Zed",
        "zed",
        "PanelCmd",
        "panelcmd",
        "same",
    ]
    seen_outcomes: set[str] = set()
    for _trial in range(500):
        skills = [
            {"name": name, "commit": f"c{index:040d}"}
            for index, name in enumerate(
                rng.sample(skill_pool, k=rng.randint(0, 3))
            )
        ]
        project_names = {skill["name"] for skill in skills}

        def random_layer(owners: list[str]) -> dict[str, str]:
            layer: dict[str, str] = {}
            for _ in range(rng.randint(0, 4)):
                if not owners:
                    break
                layer[rng.choice(command_pool)] = rng.choice(owners)
            return layer

        project_cmds = random_layer(
            [skill["name"] for skill in skills]
            or rng.sample(skill_pool, k=1)
        )
        if not project_names:
            project_cmds = {}
        global_cmds = random_layer(skill_pool)
        case_insensitive = rng.random() < 0.5

        def entry(owner: str) -> dict:
            return {
                "owner": owner,
                "target": "/manager/x",
                "kind": "script",
                "digest": {},
            }

        expected = _reference_compose(
            skills, project_cmds, global_cmds, case_insensitive
        )
        try:
            effective, suppressed = _dispatch_runtime.compose_layers(
                project_skills=skills,
                project_commands={
                    name: entry(owner) for name, owner in project_cmds.items()
                },
                global_commands={
                    name: entry(owner) for name, owner in global_cmds.items()
                },
                case_insensitive=case_insensitive,
                project_where="project at /test",
            )
        except _dispatch_runtime.CompositionError as exc:
            observed: tuple = ("error", exc.kind)
        else:
            observed = (
                "ok",
                {
                    name: (entry["owner"], from_project)
                    for name, (entry, from_project) in effective.items()
                },
                suppressed,
            )
        assert observed == expected, (
            project_cmds,
            global_cmds,
            case_insensitive,
        )
        seen_outcomes.add(expected[0] if expected[0] == "error" else "ok")
        if expected[0] == "error":
            seen_outcomes.add(expected[1])
    assert {"ok", "case_collision", "owner_collision"} <= seen_outcomes


def test_taxonomy_generated_corruption_maps_to_class(tmp_path, skills_root, csk_home):
    """Invariant 3: every corruption shape reports its taxonomy class.

    Random byte corruptions of the pointer and the registry, plus
    removals and version mutations, each produce the expected class
    through the real shim (exit code and message) and the expected
    ``DispatchError.kind`` in-process. Production call site: bare shim
    and ``_dispatch_runtime.load_registry``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-tx", {"txcmd": "TX-OK"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-tx", "tag": "v1"}])
    _registry, generation = _read_registry(csk_home)
    pointer = dispatch.current_path(csk_home)
    registry_file = dispatch.generation_registry_path(csk_home, generation)
    pointer_backup = pointer.read_bytes()
    registry_backup = registry_file.read_bytes()
    manager = str(csk_home)

    rng = random.Random(20261006)
    alphabet = b"ab_-0123456789\xff\x00\n ."
    cases: list[tuple[str, bytes]] = []
    for _ in range(12):
        cases.append(
            (
                "pointer",
                bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 40))),
            )
        )
    for _ in range(12):
        cases.append(
            (
                "registry",
                bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 120))),
            )
        )
    for index, (which, payload) in enumerate(cases):
        if which == "pointer":
            pointer.write_bytes(payload)
        else:
            registry_file.write_bytes(payload)
        try:
            proc = _bare("txcmd", cwd=project, csk_home=csk_home)
            try:
                _dispatch_runtime.load_registry(manager)
                kind: str | None = None
            except DispatchError as exc:
                kind = exc.kind
        finally:
            pointer.write_bytes(pointer_backup)
            registry_file.write_bytes(registry_backup)
        # The pointer/registry oracle: decode what the bytes must mean.
        expected_kind: str | None
        needle: str
        if which == "pointer":
            try:
                text = payload.decode("utf-8").strip()
            except UnicodeDecodeError:
                expected_kind = "registry_unreadable"
                needle = "not valid UTF-8"
            else:
                valid = 1 <= len(text) <= 128 and all(
                    ch.isascii() and (ch.isalnum() or ch in "_-") for ch in text
                )
                if not valid:
                    expected_kind = "registry_corrupt"
                    needle = "names no generation"
                elif text == generation:
                    expected_kind = None
                    needle = ""
                else:
                    expected_kind = "registry_unreadable"
                    needle = "cannot be read"
        else:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                expected_kind = "registry_unreadable"
                needle = "not valid UTF-8"
            else:
                try:
                    data = json.loads(text)
                except ValueError:
                    expected_kind = "registry_unreadable"
                    needle = "not valid JSON"
                else:
                    version = (
                        data.get("schema_version")
                        if isinstance(data, dict)
                        else None
                    )
                    has_version = (
                        isinstance(data, dict) and "schema_version" in data
                    )
                    if not isinstance(data, dict):
                        expected_kind = "registry_corrupt"
                        needle = "corrupt"
                    elif (
                        version != 1
                        or isinstance(version, bool)
                        or not has_version
                    ):
                        expected_kind = "unknown_version"
                        needle = "schema_version"
                    else:
                        # Random bytes cannot form a fully valid record;
                        # any surviving version-1 dict still fails shape.
                        expected_kind = "registry_corrupt"
                        needle = "corrupt"
        context = (index, which, payload, proc.returncode, kind)
        if expected_kind is None:
            assert proc.returncode == 0, context
            assert kind is None, context
        else:
            assert proc.returncode == 1, context
            assert "Traceback" not in proc.stderr, context
            assert "outside every registered project" not in proc.stderr, context
            assert needle in proc.stderr, context
            assert kind == expected_kind, context

    # Removals and version mutations: exact classes.
    pointer.unlink()
    try:
        proc = _bare("txcmd", cwd=project, csk_home=csk_home)
        assert proc.returncode == 1
        assert "missing" in proc.stderr
        with pytest.raises(DispatchError) as missing:
            _dispatch_runtime.load_registry(manager)
        assert missing.value.kind == "current_missing"
    finally:
        pointer.write_bytes(pointer_backup)
    registry_file.unlink()
    try:
        proc = _bare("txcmd", cwd=project, csk_home=csk_home)
        assert proc.returncode == 1
        assert "cannot be read" in proc.stderr
        with pytest.raises(DispatchError) as gone:
            _dispatch_runtime.load_registry(manager)
        assert gone.value.kind == "registry_unreadable"
    finally:
        registry_file.write_bytes(registry_backup)
    for bad_version in (2, 0, -1, "1", None, True):
        data = json.loads(registry_backup.decode("utf-8"))
        data["schema_version"] = bad_version
        registry_file.write_text(json.dumps(data), encoding="utf-8")
        try:
            proc = _bare("txcmd", cwd=project, csk_home=csk_home)
            assert proc.returncode == 1, bad_version
            assert "schema_version" in proc.stderr, bad_version
            with pytest.raises(DispatchError) as unknown:
                _dispatch_runtime.load_registry(manager)
            assert unknown.value.kind == "unknown_version"
        finally:
            registry_file.write_bytes(registry_backup)
    # A healthy registry still dispatches after the storm.
    proc = _bare("txcmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "TX-OK"


def _assert_old_or_new_surface(project, csk_home, old_generation):
    """Return "old" or "new" after asserting the surface is coherent."""
    old = _bare("oldcmd", cwd=project, csk_home=csk_home)
    new = _bare("newcmd", cwd=project, csk_home=csk_home)
    _registry, generation = _read_registry(csk_home)
    if old.returncode == 0 and old.stdout.startswith("OLD"):
        assert new.returncode == 127, (new.returncode, new.stderr)
        assert generation == old_generation
        return "old"
    assert old.returncode == 0, old.stderr
    assert old.stdout.startswith("NEW"), old.stdout
    assert new.returncode == 0, new.stderr
    assert new.stdout.startswith("NEW-EXPORT"), new.stdout
    assert generation != old_generation
    return "new"


def test_publish_state_machine_old_or_new_at_every_step(
    tmp_path, skills_root, csk_home
):
    """Invariant 2: interrupting publication at every step stays coherent.

    Each ``PUBLISH_STEPS`` fault leaves the old surface (old pin plus
    unavailable new export) or the new surface (both pins), never a
    mix; a re-run then converges to the new surface. Production call
    site: ``installer.install`` -> ``dispatch._commit_if_changed``.
    """
    for step in dispatch.PUBLISH_STEPS:
        home = tmp_path / f"machine-{step.replace(':', '-')}"
        home.mkdir()
        manager_home = home / "manager"
        skills = home / "skills"
        skills.mkdir()
        project = make_project(home, "proj-p")
        cfg = _install_tx_v1(manager_home, skills, project)
        _registry, old_generation = _read_registry(manager_home)

        def fault(wanted: str, _step: str = step) -> None:
            if wanted == _step:
                raise OSError(f"injected interruption at {_step}")

        previous = dispatch._fault_hook
        dispatch._fault_hook = fault
        try:
            results = installer.install(cfg)
        finally:
            dispatch._fault_hook = previous
        assert results[0].errors, step
        assert f"injected interruption at {step}" in results[0].errors[0], step
        observed = _assert_old_or_new_surface(project, manager_home, old_generation)

        results = installer.install(cfg)
        assert not results[0].errors, (step, results[0].errors)
        final = _assert_old_or_new_surface(project, manager_home, old_generation)
        assert final == "new", (step, observed)


def test_recovery_state_machine_old_or_new_at_every_step(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """Invariant 2 (recovery): interrupting recovery stays coherent.

    Each ``RECOVER_STEPS`` fault leaves dispatch working on the active
    generation; a re-run converges the shims (the missing shim returns,
    the stale file leaves). Production call site: ``dispatch setup``
    -> ``dispatch.recover_launchers``.
    """
    project = make_project(tmp_path, "proj-p")
    cfg = _install_tx_v1(csk_home, skills_root, project)
    results = installer.install(cfg)
    assert not results[0].errors, results[0].errors
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    for step in dispatch.RECOVER_STEPS:
        shims = dispatch.shims_dir(csk_home)
        (shims / "oldcmd").unlink()
        stale = shims / "stale-rcmd"
        stale.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")

        def fault(wanted: str, _step: str = step) -> None:
            if wanted == _step:
                raise OSError(f"injected interruption at {_step}")

        previous = dispatch._fault_hook
        dispatch._fault_hook = fault
        try:
            code = cli.main(["dispatch", "setup"])
        finally:
            dispatch._fault_hook = previous
        assert code != 0, step
        # Dispatch still works on the active generation throughout.
        proc = _bare("newcmd", cwd=project, csk_home=csk_home)
        assert proc.returncode == 0, (step, proc.stderr)
        assert proc.stdout.startswith("NEW-EXPORT")

        assert cli.main(["dispatch", "setup"]) == 0, step
        assert (shims / "oldcmd").exists(), step
        assert not stale.exists(), step
        for name, marker in (("oldcmd", "NEW"), ("newcmd", "NEW-EXPORT")):
            proc = _bare(name, cwd=project, csk_home=csk_home)
            assert proc.returncode == 0, (step, name, proc.stderr)
            assert proc.stdout.startswith(marker)


_PARITY_TARGET = """\
import base64
import json
import os
import signal
import sys


def main() -> int:
    mask_query = getattr(signal, "pthread_sigmask", None)
    report = {
        "argv": [
            base64.b64encode(
                arg.encode("utf-8", "surrogateescape")
            ).decode("ascii")
            for arg in sys.argv
        ],
        "sigpipe": int(signal.getsignal(signal.SIGPIPE)),
        "sigxfsz": int(signal.getsignal(signal.SIGXFSZ)),
        "mask": sorted(mask_query(signal.SIG_BLOCK, [])),
        "env": dict(os.environ),
    }
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "report":
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\\n")
        sys.stdout.flush()
        sys.stdout.buffer.write(b"STDIN:" + sys.stdin.buffer.read())
        sys.stdout.flush()
        sys.stderr.buffer.write(b"err\\x00\\xff\\n")
        sys.stderr.flush()
        return 37
    if mode == "signal":
        os.kill(os.getpid(), signal.SIGTERM)
        return 99
    return 98


if __name__ == "__main__":
    raise SystemExit(main())
"""

_MASKING_STUB = """\
import os
import signal
import sys

mask_set = getattr(signal, "pthread_sigmask", None)
block = getattr(signal, "SIG_BLOCK", None)
usr1 = getattr(signal, "SIGUSR1", None)
if mask_set is not None and block is not None and usr1 is not None:
    mask_set(block, [usr1])
# Simulate a clean caller: drop what this stub's own startup added.
os.environ.pop("LC_CTYPE", None)
os.environ.pop("__CF_USER_TEXT_ENCODING", None)
os.execvp(sys.argv[1], sys.argv[1:])
"""


def _scrub_parity_env(env: dict) -> dict:
    scrubbed = dict(env)
    scrubbed.pop("CSK_PROJECT_ROOT", None)
    scrubbed.pop("PATH", None)
    scrubbed.pop("_", None)
    return scrubbed


def test_exec_parity_direct_vs_dispatched(tmp_path, skills_root, csk_home):
    """Invariant 5: the target cannot tell dispatch from a direct call.

    Signal dispositions, the signal mask (under a caller-blocked
    SIGUSR1), environment, argv bytes, stdin/stdout/stderr bytes, exit
    codes and signal deaths all match between a direct execution and
    the shimmed one. Production call site: bare shim -> wrapper ->
    ``_dispatch_runtime.main`` -> ``restore_exec_signals`` /
    ``restore_caller_env`` -> ``os.execve``.
    """
    project = make_project(tmp_path, "proj-p")
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "parity": {
                        "type": "script",
                        "unix_path": "scripts/parity",
                    }
                },
            }
        ),
        "scripts/parity": (
            f"#!{sys.executable}\n".encode() + _PARITY_TARGET.encode()
        ),
    }
    make_skill_repo(skills_root, "skill-par", files, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-par", "tag": "v1"}])
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    target = entry["commands"]["parity"]["target"]

    stub = tmp_path / "masking-stub.py"
    stub.write_text(_MASKING_STUB, encoding="utf-8")
    runner = [sys.executable, str(stub)]
    shims_path = os.environ["PATH"] + os.pathsep + str(
        dispatch.shims_dir(csk_home)
    )
    tricky = [
        b"report",
        b"",
        "snowman-\u2603".encode(),
        b"\xff\xfe-binary",
        b"new\nline",
        b"quote'\"dollar$back\\slash",
        b"dash-arg",
    ]
    stdin_bytes = bytes(range(256)) * 4
    physical_cwd = os.path.realpath(project)
    bases = [
        ("scrubbed", {"PATH": shims_path}),
        (
            "caller-set",
            {
                "PATH": shims_path,
                "PWD": physical_cwd,
                "SHLVL": "5",
                "LANG": "en_US.UTF-8",
                "STICKY": "caller-value",
            },
        ),
    ]
    for base_name, base in bases:
        direct = subprocess.run(
            [*runner, target, *tricky],
            cwd=project,
            env=dict(base),
            input=stdin_bytes,
            capture_output=True,
        )
        via = subprocess.run(
            [*runner, "sh", "-c", 'exec "$@"', "sh", "parity", *tricky],
            cwd=project,
            env=dict(base),
            input=stdin_bytes,
            capture_output=True,
        )
        assert direct.returncode == 37, (base_name, direct.stderr)
        assert via.returncode == 37, (base_name, via.stderr)
        assert via.stderr == direct.stderr
        assert via.stderr == b"err\x00\xff\n"
        direct_report, _, direct_stdin = direct.stdout.partition(b"\n")
        via_report, _, via_stdin = via.stdout.partition(b"\n")
        assert via_stdin == direct_stdin == b"STDIN:" + stdin_bytes
        direct_parsed = json.loads(direct_report.decode("utf-8"))
        via_parsed = json.loads(via_report.decode("utf-8"))
        # argv[0] is the execution path by construction; the rest matches.
        assert via_parsed["argv"][1:] == direct_parsed["argv"][1:], base_name
        assert via_parsed["sigpipe"] == direct_parsed["sigpipe"], base_name
        assert via_parsed["sigxfsz"] == direct_parsed["sigxfsz"], base_name
        assert via_parsed["mask"] == direct_parsed["mask"], base_name
        assert signal.SIGUSR1 in via_parsed["mask"], base_name
        via_env = _scrub_parity_env(via_parsed["env"])
        direct_env = _scrub_parity_env(direct_parsed["env"])
        if base_name == "scrubbed":
            # Stated sh-startup bound: a /bin/sh launcher necessarily
            # materializes PWD/SHLVL when the caller lacks them, so a
            # non-sh target observes the startup values (verified
            # correct here) instead of the absence. sh targets always
            # observe identical values (see the env parity test).
            assert os.path.realpath(via_env.pop("PWD")) == physical_cwd
            # The SHLVL startup value depends on the layer count and
            # shell version; it must be a small synthesized level,
            # never leaked caller data (the caller set none).
            assert 0 <= int(via_env.pop("SHLVL")) <= 10
            assert "PWD" not in direct_env
            assert "SHLVL" not in direct_env
        assert via_env == direct_env, base_name
        assert via_parsed["env"]["CSK_PROJECT_ROOT"] == entry["canonical_root"]
        assert not [
            key for key in via_parsed["env"] if key.startswith("_CSK_ENV_")
        ], base_name

    # A PWD that does not name the cwd is normalized by sh startup on
    # the dispatch path while a direct non-sh target sees it raw; the
    # normalized value is pinned (never a guess).
    bogus = {"PATH": shims_path, "PWD": "/bogus-not-here"}
    via_bogus = subprocess.run(
        [*runner, "sh", "-c", 'exec "$@"', "sh", "parity", "report"],
        cwd=project,
        env=dict(bogus),
        capture_output=True,
    )
    assert via_bogus.returncode == 37, via_bogus.stderr
    bogus_report = json.loads(
        via_bogus.stdout.partition(b"\n")[0].decode("utf-8")
    )
    assert (
        os.path.realpath(bogus_report["env"]["PWD"]) == physical_cwd
    )

    direct_sig = subprocess.run(
        [*runner, target, "signal"],
        cwd=project,
        env={"PATH": shims_path},
        capture_output=True,
    )
    via_sig = subprocess.run(
        [*runner, "sh", "-c", 'exec "$@"', "sh", "parity", "signal"],
        cwd=project,
        env={"PATH": shims_path},
        capture_output=True,
    )
    assert (direct_sig.returncode, direct_sig.stdout, direct_sig.stderr) == (
        via_sig.returncode,
        via_sig.stdout,
        via_sig.stderr,
    )
    assert direct_sig.returncode == -signal.SIGTERM


def test_generated_wrapper_text_is_literal_property(tmp_path):
    """The wrapper execs hostile interpreter paths as opaque literals.

    Spaces, quotes, dollar, backticks and backslashes in the recorded
    interpreter or runtime path round-trip exactly through real shells
    (missing-interpreter guidance included); nothing evaluates. The
    locale sentinels snapshot the caller state in both the set and the
    unset case. Production call site:
    ``dispatch.dispatcher_wrapper_text``.
    """
    shells = ["sh", "bash"]
    if shutil.which("zsh") is not None:
        shells.append("zsh")
    fragments = [
        "plain",
        "with space",
        "quote'leaf",
        'double"quote',
        "dollar$home",
        "back`tick",
        "back\\slash",
        "semi;colon",
        "paren(th)esis",
        "star*glyph",
        "x$(touch STAMP)y",
    ]
    pair_count = 0
    for shell in shells:
        if shutil.which(shell) is None:
            continue
        for python_frag in fragments:
            for runtime_frag in ("plain", "with space", "quote'leaf"):
                stage = tmp_path / f"wrap-{shell}-{pair_count}"
                stage.mkdir()
                pair_count += 1
                python_stub = stage / f"py-{python_frag}"
                python_stub.write_text(
                    "#!/bin/sh\n"
                    "printf 'SET_LC=%s\\n' \"${_CSK_ENV_SET_LC_CTYPE-unset}\"\n"
                    "printf 'VAL_LC=%s\\n' \"${_CSK_ENV_VAL_LC_CTYPE-unset}\"\n"
                    "printf 'SET_CF=%s\\n' \"${_CSK_ENV_SET_CF-unset}\"\n"
                    "printf 'VAL_CF=%s\\n' \"${_CSK_ENV_VAL_CF-unset}\"\n"
                    "printf 'SELF:%s\\n' \"$0\"\n"
                    "printf 'ARG:%s\\n' \"$@\"\n",
                    encoding="utf-8",
                )
                python_stub.chmod(0o755)
                runtime = stage / f"rt-{runtime_frag}.py"
                runtime.write_text("# runtime\n", encoding="utf-8")
                text = dispatch.dispatcher_wrapper_text(
                    python=str(python_stub), runtime=runtime
                )
                wrapper = stage / "dispatcher"
                wrapper.write_text(text, encoding="utf-8")
                wrapper.chmod(0o755)
                env = {
                    "PATH": os.environ["PATH"],
                    "LC_CTYPE": "caller-locale",
                }
                env.pop("__CF_USER_TEXT_ENCODING", None)
                proc = subprocess.run(
                    [shell, str(wrapper), "somecmd", "a b", "c'd"],
                    cwd=stage,
                    env=env,
                    text=True,
                    capture_output=True,
                )
                assert proc.returncode == 0, (
                    shell,
                    python_frag,
                    runtime_frag,
                    proc.stderr,
                )
                assert f"SELF:{python_stub}\n" in proc.stdout
                assert not (stage / "STAMP").exists(), (shell, python_frag)
                assert f"ARG:-I\nARG:{runtime}\n" in proc.stdout
                assert "ARG:somecmd\nARG:a b\nARG:c'd\n" in proc.stdout
                assert "SET_LC=1\nVAL_LC=caller-locale\n" in proc.stdout
                assert "SET_CF=0\nVAL_CF=\n" in proc.stdout
    assert pair_count > 0

    # A missing interpreter names the repair instead of exec failing raw.
    stage = tmp_path / "wrap-missing"
    stage.mkdir()
    text = dispatch.dispatcher_wrapper_text(
        python=str(stage / "no python here"), runtime=stage / "rt.py"
    )
    wrapper = stage / "dispatcher"
    wrapper.write_text(text, encoding="utf-8")
    wrapper.chmod(0o755)
    proc = subprocess.run(
        [str(wrapper), "somecmd"],
        cwd=stage,
        env={"PATH": os.environ["PATH"]},
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0
    assert "csk dispatch setup" in proc.stderr


def test_dispatcher_wrapper_refuses_control_characters(tmp_path, monkeypatch):
    """An interpreter path with CR/LF/NUL refuses publication outright."""
    monkeypatch.setattr(sys, "executable", "/tmp/a\nb/python")
    with pytest.raises(
        dispatch.DispatchPublishError, match="control characters"
    ):
        dispatch.ensure_dispatcher_wrapper(tmp_path / "manager")


def test_stale_gone_record_reported_and_ignored(tmp_path, skills_root, csk_home):
    """A deleted root is reported (status notes, pins retained), not fatal.

    Production call sites: ``dispatch.stale_scope_notes``,
    ``dispatch.referenced_skill_commits``,
    ``dispatch.verify_project_digests``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-st", {"stale": "ST-OK"}, tag="v1")
    result = _install_project(
        csk_home, skills_root, project, [{"name": "skill-st", "tag": "v1"}]
    )
    assert not result.errors
    registry, _generation = _read_registry(csk_home)
    pin = next(iter(registry["projects"].values()))["skills"][0]
    assert pin["name"] == "skill-st"
    canonical = str(project.resolve())
    shutil.rmtree(project)

    notes = dispatch.stale_scope_notes(csk_home)
    assert len(notes) == 1
    assert canonical in notes[0]
    assert "no longer exists" in notes[0]
    assert dispatch.verify_project_digests(csk_home, [canonical]) == []
    assert (pin["name"], pin["commit"]) in dispatch.referenced_skill_commits(
        csk_home
    )

    # A replaced root (same path, new identity) is a status problem.
    project.mkdir()
    problems = dispatch.verify_project_digests(csk_home, [canonical])
    assert len(problems) == 1
    assert "no longer matches" in problems[0]
    assert "csk install" in problems[0]
