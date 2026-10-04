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
import re
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
    try:
        import fcntl

        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\x00" * 1024)
        path = bytes(raw).split(b"\x00", 1)[0].decode(
            "utf-8", "surrogateescape"
        )
        return path or None
    except (OSError, ImportError, AttributeError):
        return None


def _normalize_traced_path(
    file: object,
    *,
    dir_fd: int | None = None,
    fd_paths: dict[int, str] | None = None,
) -> str:
    """Normalize one traced open/list path to a real absolute path.

    Bytes decode with surrogateescape, ``dir_fd``-relative paths resolve
    against the descriptor target, and anything else relative resolves
    against the CWD at event time. An int descriptor resolves live
    first (event-time truth under fd reuse), then through the tracer's
    map of path-opened descriptors (so a ``fdopen`` after a tracked
    ``os.open`` attributes to the already-recorded path); a descriptor
    that is neither live-resolvable nor tracked returns a ``<...>``
    marker the tracer treats as a violation (dispatch never reads
    through untracked descriptors, so any occurrence is hostile).
    """
    if isinstance(file, int):
        resolved = _resolve_open_fd(file)
        if resolved is None and fd_paths is not None:
            resolved = fd_paths.get(file)
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
    bound). Descriptors resolve live first, then through the map of
    path-opened descriptors, so only a genuinely untracked descriptor
    stays a violation.
    """

    def __init__(self, checkout: Path) -> None:
        self.checkout = os.path.realpath(checkout)
        self.records: set[str] = set()
        self._originals: dict = {}
        self._fd_paths: dict[int, str] = {}

    def _record(self, file: object, *, dir_fd: int | None = None) -> None:
        self.records.add(
            _normalize_traced_path(
                file, dir_fd=dir_fd, fd_paths=self._fd_paths
            )
        )

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
            seen = _normalize_traced_path(
                path,
                dir_fd=kwargs.get("dir_fd"),  # type: ignore[arg-type]
                fd_paths=tracer._fd_paths,
            )
            tracer.records.add(seen)
            fd = original_os_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
            if not seen.startswith("<"):
                tracer._fd_paths[fd] = seen
            return fd

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


def _resolve_in_process(
    csk_home: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    """Run the dispatcher in RESOLVE mode in-process; return (code, out, err).

    Resolve prints the two-line shim protocol on stdout (absolute target,
    then the project root or ``-`` for global) and never execs, so
    identity tests observe selection directly instead of intercepting
    execve.
    """
    code = _dispatch_runtime.main(
        [str(dispatch.dispatcher_path(csk_home)), "resolve", command]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


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


def _install_project_allowing_errors(
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
    return installer.install(cfg)


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
        code = _dispatch_runtime.main([str(dispatcher), "resolve", "dtool"])
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

    def find(records: dict, cwd: str) -> dict | None:
        return _dispatch_runtime.find_project(
            records, cwd, manager_home=str(tmp_path)
        )

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
        _dispatch_runtime.find_project(
            projects, str(root / "sub"), manager_home=str(tmp_path)
        )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert (
        _dispatch_runtime.find_project(
            projects, str(elsewhere), manager_home=str(tmp_path)
        )
        is None
    )


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
        _dispatch_runtime.find_project(
            projects, str(nested), manager_home=str(tmp_path)
        )
    # The live parent still selects CWDs directly beneath it.
    assert (
        _dispatch_runtime.find_project(
            projects, str(parent), manager_home=str(tmp_path)
        )["checkout_id"]
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
    assert content == dispatch.shim_text(
        dispatch.dispatcher_path(csk_home), "dtool"
    )
    lines = content.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert (
        f"_CSK_DISPATCHER={dispatch.sh_single_quote(str(dispatch.dispatcher_path(csk_home)))}"
        in lines
    )
    assert "_CSK_COMMAND='dtool'" in lines
    assert (
        '_csk_out=$("$_CSK_DISPATCHER" resolve "$_CSK_COMMAND") || exit $?'
        in lines
    )
    # Save/restore around the uppercase locals, $1 handoff, scrub of
    # every lowercase working variable, then a variable-free exec: no
    # dispatcher-private value survives into the target.
    assert "_csk_have_dispatcher=${_CSK_DISPATCHER+set}" in lines
    assert "_csk_save_dispatcher=${_CSK_DISPATCHER-}" in lines
    assert "_csk_have_command=${_CSK_COMMAND+set}" in lines
    assert "_csk_save_command=${_CSK_COMMAND-}" in lines
    assert 'set -- "$_csk_target" "$@"' in lines
    assert "  export _CSK_DISPATCHER" in lines
    assert "  export _CSK_COMMAND" in lines
    assert "  unset _CSK_DISPATCHER" in lines
    assert "  unset _CSK_COMMAND" in lines
    assert (
        "unset _csk_have_dispatcher _csk_save_dispatcher "
        "_csk_have_command _csk_save_command "
        "_csk_out _csk_nl _csk_target _csk_root"
    ) in lines
    assert 'exec "$@"' in lines
    # Diagnostics name the command through printf %s, never through
    # expansion inside double quotes.
    assert 'printf "error: dispatch resolver for command %s returned ' in (
        content
    )
    assert "  export CSK_PROJECT_ROOT" in lines
    assert "  unset CSK_PROJECT_ROOT || exit 1" in lines
    for token in ("csk", "python", "python3"):
        for line in lines:
            if line.startswith("#"):
                continue
            assert f" {token} " not in f" {line} "

    wrapper = dispatch.dispatcher_path(csk_home).read_text(encoding="utf-8")
    wrapper_lines = wrapper.splitlines()
    assert wrapper_lines[0] == "#!/bin/sh"
    assert dispatch.sh_single_quote(sys.executable) in wrapper
    assert (
        dispatch.sh_single_quote(str(dispatch.dispatcher_runtime_path(csk_home)))
        in wrapper
    )
    assert (
        f"exec {dispatch.sh_single_quote(sys.executable)} -I "
        f"{dispatch.sh_single_quote(str(dispatch.dispatcher_runtime_path(csk_home)))} "
        '"$@"'
    ) in wrapper_lines
    assert "csk dispatch setup" in wrapper
    # No dispatcher-private variable is set in the caller's environment:
    # the wrapper holds no shell variables at all, so it exports
    # nothing and cannot clobber caller-owned names.
    assert not [
        line for line in wrapper_lines if line.startswith("export ")
    ], wrapper
    assert "_CSK_ENV_" not in wrapper
    assert "_CSK_PYTHON" not in wrapper
    assert "_CSK_RUNTIME" not in wrapper
    assert not [
        line
        for line in wrapper_lines
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line)
    ], wrapper

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


def test_renamed_root_uses_global_until_reregistered(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """A moved-away root no longer matches: fall through, report, repair.

    Revision 4 removed the inode-only fallback (an inode match without
    a path match is never a match), so the renamed directory falls
    through to the global fallback instead of refusing: the stale
    record cannot select its pin from an unmatched path. Status still
    reports the stale record, and reinstalling at the new path restores
    the pin (see ``test_r4_renamed_project_reregistered_keeps_dispatch_working``).
    A same-path replacement still refuses (see
    ``test_replaced_root_refuses_with_reregister_guidance``): only a
    proven containing root fails closed. Production call sites: bare
    shim -> ``_dispatch_runtime.find_project`` and
    ``dispatch.verify_project_digests``.
    """
    project = make_project(tmp_path, "proj-p")
    repo, _commit = _make_command_skill(
        skills_root, "skill-p", {"ptool": "PROJECT-P"}, tag="v1"
    )
    _retitle_skill(repo, {"ptool": "GLOBAL-P"}, tag="v2")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v2"}])
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))

    moved = tmp_path / "proj-moved"
    project.rename(moved)
    proc = _bare("ptool", cwd=moved, csk_home=csk_home)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.splitlines()[0] == "GLOBAL-P"
    assert "PROJECT-P" not in proc.stdout

    # The stale record is reported, never silent: status names it
    # and fails the health check, like any other broken project.
    assert cli.main(["status", "app", "--check"]) == 1
    out = capsys.readouterr().out
    assert "no longer exists" in out, out


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
        "needs_migration": False,
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
    """Generated shell text treats hostile paths and names as literals.

    The setup snippet and, for every hostile manager path plus hostile
    command-name pair, the resolve-then-exec shim: the stub dispatcher
    receives the exact command name, the target runs with exact argv and
    the resolved root, nothing evaluates (no MARKER), and malformed
    resolve output refuses instead of execing. Production call sites:
    ``dispatch.setup_snippet`` and ``dispatch.shim_text``.
    """
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
        target = manager / "probe-target"
        target.write_text(
            "#!/bin/sh\n"
            'printf "ARG:%s\\n" "$@"\n'
            'printf "GOT-ROOT:%s\\n" "${CSK_PROJECT_ROOT-unset}"\n',
            encoding="utf-8",
        )
        target.chmod(0o755)
        stub = manager / "stub-dispatcher"
        stub.write_text(
            "#!/bin/sh\n"
            'printf "RESOLVED:%s\\n" "$*" >> "$STUB_LOG"\n'
            'printf "%s\\n%s\\n" "$STUB_TARGET" "$STUB_ROOT"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        shim = manager / "probe-shim"
        shim.write_text(dispatch.shim_text(stub, name_frag), encoding="utf-8")
        shim.chmod(0o755)
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
            log = case_dir / f"resolved-{Path(shell).name}-{index}.log"
            if log.exists():
                log.unlink()
            launched = subprocess.run(
                [shell, str(shim), "extra arg", ""],
                cwd=case_dir,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": "/usr/bin:/bin",
                    "STUB_TARGET": str(target),
                    "STUB_ROOT": str(case_dir),
                    "STUB_LOG": str(log),
                },
            )
            assert not marker.exists(), (shell, name_frag)
            # The stub dispatcher receives the exact command name as one
            # argument (raw-text comparison: a newline in the name cannot
            # round-trip a line log, but it must still arrive intact).
            assert log.read_text(encoding="utf-8") == (
                f"RESOLVED:resolve {name_frag}\n"
            ), (shell, name_frag)
            if "\n" in path_frag:
                # The target path itself carries a newline, which the
                # line-based protocol cannot represent (publication
                # refuses such paths): the shim must refuse instead of
                # execing a truncated guess.
                assert launched.returncode == 1, (shell, name_frag, launched)
                assert "malformed" in launched.stderr, (shell, name_frag)
                assert "ARG:" not in launched.stdout, (shell, name_frag)
                continue
            assert launched.returncode == 0, (shell, name_frag, launched.stderr)
            assert launched.stdout.splitlines() == [
                "ARG:extra arg",
                "ARG:",
                f"GOT-ROOT:{case_dir}",
            ], (shell, name_frag, launched.stdout)
            # Malformed resolve output refuses instead of execing a guess.
            for bad_target, bad_root in (
                ("", str(case_dir)),
                ("single-line", "MISSING-NEWLINE"),
            ):
                env = {
                    **os.environ,
                    "PATH": "/usr/bin:/bin",
                    "STUB_TARGET": bad_target,
                    "STUB_ROOT": bad_root,
                    "STUB_LOG": str(log),
                }
                if bad_root == "MISSING-NEWLINE":
                    broken = manager / "broken-dispatcher"
                    broken.write_text(
                        "#!/bin/sh\nprintf 'one-line-no-newline'\n",
                        encoding="utf-8",
                    )
                    broken.chmod(0o755)
                    broken_shim = manager / "broken-shim"
                    broken_shim.write_text(
                        dispatch.shim_text(broken, name_frag), encoding="utf-8"
                    )
                    broken_shim.chmod(0o755)
                    refused = subprocess.run(
                        [shell, str(broken_shim)],
                        cwd=case_dir,
                        capture_output=True,
                        text=True,
                        env=env,
                    )
                else:
                    refused = subprocess.run(
                        [shell, str(shim)],
                        cwd=case_dir,
                        capture_output=True,
                        text=True,
                        env=env,
                    )
                assert refused.returncode == 1, (shell, name_frag, refused)
                assert "malformed" in refused.stderr, (shell, name_frag)
                assert "ARG:" not in refused.stdout, (shell, name_frag)


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
    tmp_path, skills_root, csk_home, capsys
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

    def run_resolve(command: str, cwd: Path, tracer: _CheckoutReadTracer):
        here = os.getcwd()
        os.chdir(cwd)
        try:
            with tracer:
                return _resolve_in_process(csk_home, command, capsys)
        finally:
            os.chdir(here)

    project_tracer = _CheckoutReadTracer(project)
    code, out, err = run_resolve("htool", project, project_tracer)
    assert code == 0, err
    assert out.splitlines() == [recorded_target, canonical]

    hostile_tracer = _CheckoutReadTracer(hostile)
    code, out, err = run_resolve("gtool", hostile, hostile_tracer)
    assert code == 0, err
    assert out.splitlines() == [global_target, "-"]

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
    selection + target validation, the exact ``resolve_command``
    sequence the shim runs before execing the target), p95 <= 50ms at
    1/10/100 registered projects. The end-to-end shim->dispatcher->target
    p95 is MEASURED and REPORTED (print, step summary, warnings summary)
    but never asserted: its tail is sh+python spawn scheduling (~30ms
    typical, 50-90ms on loaded macOS; direct-exec baseline ~3ms), not
    dispatch logic.
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
                _dispatch_runtime.select_command(
                    registry, cwd, "latcmd", manager_home=manager
                )
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

    The shim execs the resolved target directly, so the caller's
    default disposition reaches it untouched. Production call site:
    bare shim -> ``dispatcher resolve`` -> ``exec "$target"``.
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


def test_r3_single_skipped_project_publishes_empty_scope_immediately(
    tmp_path, skills_root, csk_home
):
    """F3: a lone skipped install still publishes its boundary at once.

    With a single configured project and no Skillfile, the install
    skips before any legacy scan could run (the scan only runs after a
    successful install), so the skip path itself must publish the
    empty scope: the record exists immediately, with no pins. This
    pins ``installer.install`` -> ``_publish_empty_dispatch_scope``
    alone; the nested variant above is additionally covered by the
    legacy scan. Single-case killer for the skip-scope narrowing
    mutant.
    """
    from csk.config import GlobalConfig, ProjectConfig

    project = make_project(tmp_path, "lonely")
    (project / "Skillfile.json").unlink(missing_ok=True)
    cfg = GlobalConfig(
        path=csk_home / "config.json",
        skills_root=skills_root,
        preferred_locale="ru",
        default_agents=["claude_code"],
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={
            "lonely": ProjectConfig(
                alias="lonely", path=project, agents=["claude_code"]
            ),
        },
    )
    results = installer.install(cfg)
    assert len(results) == 1
    assert results[0].status == "skipped", results[0].messages
    assert not results[0].errors, results[0].errors
    registry, _generation = _read_registry(csk_home)
    entry = _entry_for_root(registry, project.resolve())
    assert entry["commands"] == {}
    assert entry["skills"] == []


def test_r4_remount_simulation_ignores_device_change(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """F4: a bare st_dev change never breaks root matching.

    The matcher reads inodes, never device numbers; this simulates the
    panel's native APFS remount (same root, same inode, new device) by
    wrapping ``os.stat`` in-process and driving production RESOLVE
    mode. Production call site: ``_dispatch_runtime.main`` ->
    ``find_project``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-rv", {"rvcmd": "RV-OK"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-rv", "tag": "v1"}])
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    recorded_target = entry["commands"]["rvcmd"]["target"]

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

    monkeypatch.setattr(_dispatch_runtime.os, "stat", remounted_stat)
    here = os.getcwd()
    os.chdir(project)
    try:
        code, out, err = _resolve_in_process(csk_home, "rvcmd", capsys)
    finally:
        os.chdir(here)
    assert code == 0, err
    assert out.splitlines() == [recorded_target, entry["canonical_root"]]


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
    ``CSK_PROJECT_ROOT``: no ``LC_CTYPE`` coercion leak and no
    dispatcher-private variable. The shim execs the target directly,
    so nothing is added or removed apart from the registry-derived
    root. Production call site: bare shim -> ``dispatcher resolve`` ->
    ``exec "$target"``.
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
    # The command name must be unshadowable on every CI OS: "gold" is
    # /usr/bin/gold (binutils) on ubuntu and trips the _bare guard.
    repo, _commit = _make_command_skill(
        skills_root, "skill-gold", {"n15cmd": "GOLD-V1"}, tag="v1"
    )
    _retitle_skill(repo, {"n15cmd": "GOLD-V2"}, tag="v2")
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
    proc = _bare("n15cmd", cwd=outside, csk_home=csk_home)
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


def _reference_identity_match(
    records, cwd_parts, ino_of, recorded, *, case_insensitive
):
    """Naive oracle for root matching, written from the invariant text.

    ``records`` maps id -> root parts list; ``ino_of`` maps an absolute
    path string to its live inode (missing when the path is gone);
    ``recorded`` maps id -> recorded inode. ``case_insensitive`` is the
    volume's actual rule for this trial. Returns ("match", id) |
    ("stale", id) | ("contradictory",) | ("outside",).

    Path-prefix containment under the given rule decides the deepest
    candidates; the recorded inode then confirms liveness. An inode
    coincidence without a path match selects nothing: there is no
    inode-only fallback. A deepest candidate that is gone or replaced
    refuses; two live candidates for one directory refuse as
    contradictory.
    """

    def same(left, right):
        return left == right or (
            case_insensitive and left.casefold() == right.casefold()
        )

    def prefix_equal(record_parts, wanted):
        if len(record_parts) > len(wanted):
            return False
        return all(
            same(left, right)
            for left, right in zip(record_parts, wanted[: len(record_parts)])
        )

    def live_ino(root):
        # Kernel truth, independent of the comparison rule: stating the
        # recorded spelling succeeds exactly when the kernel resolves it
        # (case aliases resolve on insensitive volumes, nowhere else).
        # Only the inode is read, so device-shifting stat wrappers used
        # by remount trials cannot disturb the oracle.
        try:
            return os.stat(root).st_ino
        except OSError:
            return None

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
    if not candidates:
        return ("outside",)
    live = []
    for checkout_id in candidates:
        root = "/" + "/".join(records[checkout_id])
        if live_ino(root) == recorded[checkout_id]:
            live.append(checkout_id)
    if len(live) == 1:
        return ("match", live[0])
    if len(live) > 1:
        return ("contradictory",)
    return ("stale", candidates[0])


def test_identity_generated_trees_match_reference(tmp_path, monkeypatch):
    """Invariant 1: random trees agree with the reference model.

    Symlinks, case-variant spellings, moved and replaced roots,
    cross-root inode coincidences, lookalike prefixes and remount
    simulation (device numbers rewritten under the matcher): every
    scenario matches the naive oracle under a forced case-sensitive
    rule, a forced case-insensitive rule, and the volume's actual
    probed rule, with and without the device rewrite. Agreement under
    both forced rules proves neither blanket casefolding nor blanket
    exact matching: the rule comes from the volume's oracle. The
    coincidence trials close the inode-only class: a recorded inode
    that only matches an unrelated root never selects. Production
    call site: ``_dispatch_runtime.find_project``.
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

    def check_agreement(
        label, raw_cwd, records, record_paths, cwd_parts, ino_of, recorded
    ):
        physical_cwd = os.path.realpath(raw_cwd)
        actual_rule = _dispatch_runtime.volume_case_insensitive(
            physical_cwd, manager_home=str(tmp_path)
        )
        for rule in (False, True, actual_rule):
            expected = _reference_identity_match(
                record_paths,
                cwd_parts,
                ino_of,
                recorded,
                case_insensitive=rule,
            )
            monkeypatch.setattr(
                _dispatch_runtime,
                "volume_case_insensitive",
                lambda *args, **kwargs: rule,
            )
            for wrap_stat in (False, True):
                if wrap_stat:
                    monkeypatch.setattr(
                        _dispatch_runtime.os, "stat", dev_shifting_stat
                    )
                else:
                    monkeypatch.setattr(
                        _dispatch_runtime.os, "stat", real_stat
                    )
                try:
                    found = _dispatch_runtime.find_project(
                        records, raw_cwd, manager_home=str(tmp_path)
                    )
                except DispatchError as exc:
                    if exc.kind == "stale_root":
                        observed: tuple = ("stale", "")
                    elif exc.kind == "contradictory":
                        observed = ("contradictory",)
                    else:
                        raise AssertionError(
                            f"trial {label}: unexpected kind {exc.kind}"
                        ) from exc
                else:
                    if found is None:
                        observed = ("outside",)
                    else:
                        observed = ("match", found["checkout_id"])
                context = (label, raw_cwd, expected, observed, rule, wrap_stat)
                assert observed[0] == expected[0], context
                if expected[0] == "match":
                    assert observed[1] == expected[1], context
                seen.add(expected[0])

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
        disk_paths: dict[str, str] = {}
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
            disk_paths[checkout_id] = str(root)
            record_paths[checkout_id] = [
                part for part in str(root).split("/") if part
            ]
        # Sometimes respell a record by case only (same directory,
        # different spelling): on an insensitive volume the kernel
        # still resolves the alias; on a sensitive one the spelling is
        # gone. Both forced rules must agree with the kernel.
        if records and rng.random() < 0.25:
            victim = rng.choice(sorted(records))
            swapped = "/".join(
                part.swapcase()
                for part in records[victim]["canonical_root"].split("/")
            )
            if swapped != records[victim]["canonical_root"]:
                records[victim]["canonical_root"] = swapped
                record_paths[victim] = [
                    part for part in swapped.split("/") if part
                ]
        # Sometimes forge an inode coincidence: the record keeps its
        # path but claims another live directory's inode, the shape an
        # inode-only fallback would select from the wrong subtree.
        if len(records) >= 2 and rng.random() < 0.25:
            first, second = rng.sample(sorted(records), k=2)
            records[first]["root_identity"] = {
                "st_ino": records[second]["root_identity"]["st_ino"]
            }
            recorded[first] = recorded[second]
        # Rarely, a duplicated registration of one directory (the
        # hand-corruption shape publication dedupes away).
        if records and rng.random() < 0.06:
            donor = rng.choice(sorted(records))
            twin = f"{trial:04d}9{'7' * 59}"
            records[twin] = dict(records[donor])
            records[twin]["checkout_id"] = twin
            recorded[twin] = recorded[donor]
            record_paths[twin] = record_paths[donor]
        # Mutations: rename, replace, or delete recorded roots. The
        # mutation always addresses the on-disk path: renaming through a
        # case-alias spelling is rejected on some filesystems even where
        # stating through it succeeds.
        for checkout_id in list(records):
            roll = rng.random()
            root = Path(
                disk_paths.get(
                    checkout_id, records[checkout_id]["canonical_root"]
                )
            )
            if not root.exists() or root == case:
                continue
            if roll < 0.15:
                root.rename(case / f"moved-{checkout_id[-6:]}")
            elif roll < 0.25:
                shutil.rmtree(root)
                root.mkdir()
            elif roll < 0.32:
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
        check_agreement(
            trial, raw_cwd, records, record_paths, cwd_parts, ino_of, recorded
        )
    # Deterministic stale trial: the generated loop reaches "stale"
    # only through replace-with-fresh-inode, which an
    # inode-recycling filesystem (CI ubuntu) may never yield in 150
    # trials. Rename-away plus recreate forces a fresh inode on every
    # filesystem (the old inode stays allocated), pinning the class
    # while still asserting model agreement.
    forced = tmp_path / "ident-forced-stale"
    forced.mkdir()
    forced = forced.resolve()
    stale_root = forced / "root"
    stale_root.mkdir()
    stale_ino = stale_root.stat().st_ino
    stale_root.rename(forced / "root-orig")
    stale_root.mkdir()
    assert stale_root.stat().st_ino != stale_ino
    forced_records = {
        "forcedstale": {
            "checkout_id": "forcedstale",
            "canonical_root": str(stale_root),
            "root_identity": {"st_ino": str(stale_ino)},
        }
    }
    forced_paths = {
        "forcedstale": [part for part in str(stale_root).split("/") if part]
    }
    forced_ino_of = {
        str(forced): forced.stat().st_ino,
        str(forced / "root-orig"): (forced / "root-orig").stat().st_ino,
        str(stale_root): stale_root.stat().st_ino,
    }
    forced_cwd_parts = list(forced_paths["forcedstale"])
    forced_recorded = {"forcedstale": stale_ino}
    for forced_rule in (False, True):
        assert _reference_identity_match(
            forced_paths,
            forced_cwd_parts,
            forced_ino_of,
            forced_recorded,
            case_insensitive=forced_rule,
        ) == ("stale", "forcedstale")
    check_agreement(
        "forced-stale",
        str(stale_root),
        forced_records,
        forced_paths,
        forced_cwd_parts,
        forced_ino_of,
        forced_recorded,
    )
    # Deterministic contradictory trial: twin live registrations of one
    # directory refuse under both forced rules.
    clash = tmp_path / "ident-forced-clash"
    clash.mkdir()
    clash = clash.resolve()
    clash_ino = clash.stat().st_ino
    clash_records = {
        "a" * 64: {
            "checkout_id": "a" * 64,
            "canonical_root": str(clash),
            "root_identity": {"st_ino": str(clash_ino)},
        },
        "b" * 64: {
            "checkout_id": "b" * 64,
            "canonical_root": str(clash),
            "root_identity": {"st_ino": str(clash_ino)},
        },
    }
    clash_paths = {
        "a" * 64: [part for part in str(clash).split("/") if part],
        "b" * 64: [part for part in str(clash).split("/") if part],
    }
    clash_ino_of = {str(clash): clash_ino}
    for forced_rule in (False, True):
        assert _reference_identity_match(
            clash_paths,
            list(clash_paths["a" * 64]),
            clash_ino_of,
            {"a" * 64: clash_ino, "b" * 64: clash_ino},
            case_insensitive=forced_rule,
        ) == ("contradictory",)
    check_agreement(
        "forced-clash",
        str(clash),
        clash_records,
        clash_paths,
        list(clash_paths["a" * 64]),
        clash_ino_of,
        {"a" * 64: clash_ino, "b" * 64: clash_ino},
    )
    assert {"match", "stale", "outside", "contradictory"} <= seen, seen


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
    mix; a re-run then converges to the new surface. Faults also fire
    inside the atomic writer itself (after temp write, after fsync,
    before rename) on the generation pointer, the one file every
    resolve depends on and the one a live-unlink-first writer would
    strand. Production call site: ``installer.install`` ->
    ``dispatch._commit_if_changed``.
    """
    fault_points = [
        *dispatch.PUBLISH_STEPS,
        *(f"{phase}:current" for phase in dispatch.ATOMIC_STEPS),
    ]
    for step in fault_points:
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
    tmp_path, skills_root, stable_env, monkeypatch
):
    """Invariant 2 (recovery): interrupting recovery stays coherent.

    Three phases. Setup reconciliation (``RECOVER_STEPS``) keeps the
    active generation working and converges on retry. Explicit
    ``dispatch recover`` on a corrupt active registry
    (``ACTIVATE_STEPS`` plus one inner atomic fault) leaves either the
    corrupt state or the recovered older generation, never a mix, and
    a retry converges to the recovered generation. Reset
    (``recover:remove_corrupt``) activates the fresh empty generation
    even when removal is interrupted, and a reinstall afterwards
    works. Production call sites: ``dispatch setup`` ->
    ``dispatch.recover_launchers`` and ``dispatch recover`` ->
    ``dispatch.recover_registry``.
    """
    _make_command_skill(skills_root, "skill-tx", {"oldcmd": "TX-V1"}, tag="v1")
    _make_command_skill(
        skills_root, "skill-tx", {"oldcmd": "NEW", "newcmd": "NEW-EXPORT"}, tag="v2"
    )

    def fault_for(wanted: str):
        def fault(step: str) -> None:
            if wanted == step:
                raise OSError(f"injected interruption at {wanted}")

        return fault

    def run_cli(args: list[str]):
        return cli.main(args)

    # Phase 1: setup reconciliation faults keep the active generation.
    setup_home = tmp_path / "recover-setup"
    setup_home.mkdir()
    setup_project = make_project(setup_home, "proj-p")
    (setup_home / "skills").mkdir()
    setup_cfg = _install_tx_v1(
        setup_home / "manager", setup_home / "skills", setup_project
    )
    assert not installer.install(setup_cfg)[0].errors
    save_config(setup_cfg)
    monkeypatch.setenv("CSK_CONFIG", str(setup_cfg.path))
    for step in dispatch.RECOVER_STEPS:
        manager = setup_home / "manager"
        shims = dispatch.shims_dir(manager)
        (shims / "oldcmd").unlink()
        stale = shims / "stale-rcmd"
        stale.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        previous = dispatch._fault_hook
        dispatch._fault_hook = fault_for(step)
        try:
            code = run_cli(["dispatch", "setup"])
        finally:
            dispatch._fault_hook = previous
        assert code != 0, step
        proc = _bare("newcmd", cwd=setup_project, csk_home=manager)
        assert proc.returncode == 0, (step, proc.stderr)
        assert proc.stdout.startswith("NEW-EXPORT")
        assert run_cli(["dispatch", "setup"]) == 0, step
        assert (shims / "oldcmd").exists(), step
        assert not stale.exists(), step

    # Phase 2: explicit recover faults leave corrupt-or-recovered.
    # Two different skills (not two pins of one): a same-skill upgrade
    # prunes the older pin's targets, while the older generation under
    # test must stay activatable.
    for step in [*dispatch.ACTIVATE_STEPS, "atomic:after_write:current"]:
        home = tmp_path / f"recover-{step.replace(':', '-')}"
        home.mkdir()
        manager = home / "manager"
        locking.provision_new_manager_home(manager)
        phase_skills = home / "skills"
        phase_skills.mkdir()
        project = make_project(home, "proj-p")
        _make_command_skill(phase_skills, "skill-a", {"oldcmd": "OLD"}, tag="v1")
        _install_project(
            manager, phase_skills, project, [{"name": "skill-a", "tag": "v1"}]
        )
        # A keeper project holds the v1 pin's targets alive across the
        # v2 install (otherwise pruning removes them and the recovered
        # generation's pin is legitimately broken).
        keeper = make_project(home, "keeper")
        _install_project(
            manager, phase_skills, keeper, [{"name": "skill-a", "tag": "v1"}]
        )
        _make_command_skill(
            phase_skills, "skill-b", {"newcmd": "NEW-EXPORT"}, tag="v1"
        )
        _install_project(
            manager, phase_skills, project, [{"name": "skill-b", "tag": "v1"}]
        )
        cfg = make_config(manager, phase_skills, project, agents=["claude_code"])
        save_config(cfg)
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        _registry, corrupt_generation = _read_registry(manager)
        older = [
            child.name
            for child in sorted(dispatch.generations_dir(manager).iterdir())
            if child.name != corrupt_generation
        ]
        assert older, "the v1 generation must survive for recovery"
        active_registry = (
            dispatch.generations_dir(manager)
            / corrupt_generation
            / "registry.json"
        )
        active_registry.write_bytes(b"{broken")
        previous = dispatch._fault_hook
        dispatch._fault_hook = fault_for(step)
        try:
            code = run_cli(["dispatch", "recover"])
        finally:
            dispatch._fault_hook = previous
        assert code != 0, step
        _assert_recovered_or_corrupt(
            project, manager, corrupt_generation, step=step
        )
        assert run_cli(["dispatch", "recover"]) == 0, step
        recovered, recovered_generation = _read_registry(manager)
        assert recovered_generation != corrupt_generation, step
        proc = _bare("oldcmd", cwd=project, csk_home=manager)
        assert proc.returncode == 0, (step, proc.stderr)
        assert proc.stdout.startswith("OLD"), (step, proc.stdout)
        proc = _bare("newcmd", cwd=project, csk_home=manager)
        assert proc.returncode == 127, (step, proc.returncode, proc.stderr)

    # Phase 3: reset faults still activate the empty generation, and a
    # reinstall afterwards works.
    home = tmp_path / "recover-reset"
    home.mkdir()
    manager = home / "manager"
    locking.provision_new_manager_home(manager)
    phase_skills = home / "skills"
    phase_skills.mkdir()
    project = make_project(home, "proj-p")
    _make_command_skill(phase_skills, "skill-a", {"oldcmd": "OLD"}, tag="v1")
    _install_project(
        manager, phase_skills, project, [{"name": "skill-a", "tag": "v1"}]
    )
    cfg = make_config(manager, phase_skills, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    proc = _bare("oldcmd", cwd=project, csk_home=manager)
    assert proc.returncode == 0, proc.stderr
    for child in dispatch.generations_dir(manager).iterdir():
        (child / "registry.json").write_bytes(b"\xff")
    previous = dispatch._fault_hook
    dispatch._fault_hook = fault_for("recover:remove_corrupt")
    try:
        code = run_cli(["dispatch", "recover", "--reset"])
    finally:
        dispatch._fault_hook = previous
    assert code != 0, "the injected removal fault must fail the reset"
    _registry, empty_generation = _read_registry(manager)
    proc = _bare("oldcmd", cwd=project, csk_home=manager)
    assert proc.returncode == 127, (proc.returncode, proc.stderr)
    assert run_cli(["dispatch", "recover", "--reset"]) == 0
    remaining = [
        child.name
        for child in dispatch.generations_dir(manager).iterdir()
    ]
    assert remaining == [empty_generation], remaining
    results = installer.install(cfg)
    assert not results[0].errors, results[0].errors
    proc = _bare("oldcmd", cwd=project, csk_home=manager)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("OLD")


def _assert_recovered_or_corrupt(
    project: Path, manager: Path, corrupt_generation: str, *, step: str
) -> None:
    """Assert the surface is one coherent recovery state, never a mix.

    While the corrupt generation is still active, every staged shim
    reports the registry error (exit 1) and unstaged names stay
    missing (exit 127): a fault before staging leaves the v2 shims
    (only ``newcmd``), a fault after staging leaves both. Once the
    pointer switches, the recovered v1 pin serves and the v2-only
    export is unavailable.
    """
    current = (
        (dispatch.dispatch_dir(manager) / "current")
        .read_text(encoding="utf-8")
        .strip()
    )
    shims = dispatch.shims_dir(manager)
    old = _bare("oldcmd", cwd=project, csk_home=manager)
    new = _bare("newcmd", cwd=project, csk_home=manager)
    if current == corrupt_generation:
        assert (shims / "newcmd").exists(), step
        assert new.returncode == 1, (step, new.returncode, new.stderr)
        if (shims / "oldcmd").exists():
            assert old.returncode == 1, (step, old.returncode, old.stderr)
        else:
            assert old.returncode == 127, (step, old.returncode, old.stderr)
        return
    assert current != corrupt_generation, step
    assert old.returncode == 0, (step, old.returncode, old.stderr)
    assert old.stdout.startswith("OLD"), (step, old.stdout)
    assert new.returncode == 127, (step, new.returncode, new.stderr)


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


_PARITY_SH_SIGNAL_TARGET = """\
#!/bin/sh
kill -PIPE $$
printf 'SURVIVED\\n'
env -0
"""


def test_exec_parity_direct_vs_dispatched(tmp_path, skills_root, csk_home):
    """Invariant 5: the target cannot tell dispatch from a direct call.

    Signal dispositions, the signal mask (under a caller-blocked
    SIGUSR1), environment, argv bytes, stdin/stdout/stderr bytes, exit
    codes and signal deaths all match between a direct execution and
    the shimmed one. The shim execs the resolved target directly, so
    caller dispositions (ignored AND default), the mask and the
    environment pass through untouched; a Python target cannot observe
    inherited SIGPIPE (its own startup resets it), so the
    ignored/default disposition legs use a shell target. Production
    call site: bare shim -> ``dispatcher resolve`` -> ``exec``.
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
            # materializes PWD when the caller lacks it (POSIX), so a
            # non-sh target observes the startup value (verified
            # correct here) instead of the absence. sh targets always
            # observe identical values (see the env parity test).
            assert os.path.realpath(via_env.pop("PWD")) == physical_cwd
            # SHLVL synthesis depends on the shell: bash-derived sh
            # sets a small level, dash leaves it absent (ubuntu). Both
            # are correct startup behavior -- never leaked caller data
            # (the caller set none) -- and absence is exact parity.
            shlvl = via_env.pop("SHLVL", None)
            if shlvl is not None:
                assert 0 <= int(shlvl) <= 10
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

    # Inherited SIGPIPE, ignored and default, through a shell target
    # (the only kind that can observe it). The legs run without the
    # masking stub so the caller's disposition is exactly the one set
    # in the child before exec; the surviving leg also dumps its
    # environment for a byte comparison.
    sig_files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "sigtool": {
                        "type": "script",
                        "unix_path": "scripts/sigtool",
                    }
                },
            }
        ),
        "scripts/sigtool": _PARITY_SH_SIGNAL_TARGET.encode(),
    }
    make_skill_repo(skills_root, "skill-sig", sig_files, tag="v1")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [
                {"name": "skill-par", "tag": "v1"},
                {"name": "skill-sig", "tag": "v1"},
            ],
        },
    )
    sig_cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    sig_results = installer.install(sig_cfg)
    assert not sig_results[0].errors, sig_results[0].errors
    sig_registry, _sig_generation = _read_registry(csk_home)
    sig_entry = next(iter(sig_registry["projects"].values()))
    sh_target = sig_entry["commands"]["sigtool"]["target"]
    sig_base = {"PATH": shims_path, "STICKY": "caller-value"}
    for ignored in (False, True):
        label = "ignored" if ignored else "default"

        def fix_sigpipe() -> None:
            signal.signal(
                signal.SIGPIPE,
                signal.SIG_IGN if ignored else signal.SIG_DFL,
            )

        direct_sh = subprocess.run(
            [sh_target],
            cwd=project,
            env=dict(sig_base),
            capture_output=True,
            preexec_fn=fix_sigpipe,
        )
        via_sh = subprocess.run(
            ["sh", "-c", 'exec "$@"', "sh", "sigtool"],
            cwd=project,
            env=dict(sig_base),
            capture_output=True,
            preexec_fn=fix_sigpipe,
        )
        if not ignored:
            assert (direct_sh.returncode, direct_sh.stdout) == (
                via_sh.returncode,
                via_sh.stdout,
            ), label
            assert direct_sh.returncode == -signal.SIGPIPE, label
            assert direct_sh.stdout == b"", label
            continue
        assert direct_sh.returncode == via_sh.returncode == 0, (
            label,
            direct_sh.stderr,
            via_sh.stderr,
        )
        first, _, direct_blob = direct_sh.stdout.partition(b"\n")
        assert first == b"SURVIVED", label
        via_first, _, via_blob = via_sh.stdout.partition(b"\n")
        assert via_first == b"SURVIVED", label

        def parse_env(blob: bytes) -> dict[str, str]:
            parsed: dict[str, str] = {}
            for item in blob.split(b"\0"):
                if not item or b"=" not in item:
                    continue
                key, _, value = item.partition(b"=")
                parsed[key.decode()] = value.decode()
            return parsed

        direct_sig_env = parse_env(direct_blob)
        via_sig_env = parse_env(via_blob)
        assert (
            via_sig_env.pop("CSK_PROJECT_ROOT")
            == sig_entry["canonical_root"]
        ), label
        assert "CSK_PROJECT_ROOT" not in direct_sig_env, label
        for worker in (direct_sig_env, via_sig_env):
            worker.pop("_", None)
            worker.pop("PATH", None)
        # Both legs are shells, so both apply the same startup rules to
        # the same absent PWD/SHLVL: identical values, including the
        # materialized PWD (pinned correct, never a guess).
        assert (
            os.path.realpath(direct_sig_env["PWD"]) == physical_cwd
        ), label
        assert via_sig_env == direct_sig_env, label
        assert not [
            key for key in via_sig_env if key.startswith("_CSK_")
        ], label


def test_generated_wrapper_text_is_literal_property(tmp_path):
    """The wrapper execs hostile interpreter paths as opaque literals.

    Spaces, quotes, dollar, backticks and backslashes in the recorded
    interpreter or runtime path round-trip exactly through real shells
    (missing-interpreter guidance included); nothing evaluates. The
    wrapper exports nothing: caller locale variables pass through to
    the runtime untouched and no dispatcher-private variable appears.
    Production call site: ``dispatch.dispatcher_wrapper_text``.
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
                    "printf 'LC=%s\\n' \"${LC_CTYPE-unset}\"\n"
                    "printf 'CSK=%s\\n' \"${_CSK_PYTHON-unset}\"\n"
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
                    "_CSK_PYTHON": "caller-owned",
                }
                proc = subprocess.run(
                    [shell, str(wrapper), "resolve", "somecmd"],
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
                assert "ARG:resolve\nARG:somecmd\n" in proc.stdout
                assert "LC=caller-locale\n" in proc.stdout
                assert "CSK=caller-owned\n" in proc.stdout
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

    # A replaced root (same path, new identity) is a status problem --
    # unless the filesystem recycled the directory inode (stated bound,
    # see test_replaced_root_refuses_with_reregister_guidance), in
    # which case the replacement is indistinguishable from the
    # original root and correctly reports no problem.
    project.mkdir()
    recorded_ino = next(iter(registry["projects"].values()))["root_identity"][
        "st_ino"
    ]
    problems = dispatch.verify_project_digests(csk_home, [canonical])
    if str(project.stat().st_ino) == recorded_ino:
        assert problems == []
    else:
        assert len(problems) == 1
        assert "no longer matches" in problems[0]
        assert "csk install" in problems[0]


# ---------------------------------------------------------------------------
# Revision 4: resolve-then-exec shims, volume-aware identity without an
# inode-only fallback, one recovery path, cross-project shim-name union,
# no-follow registry reads, and the pre-upgrade migration scope. Every
# revision-3 panel reproduction becomes a named passing test below; each
# docstring cites its source probe.
# ---------------------------------------------------------------------------


def test_resolve_usage_refuses_non_resolve_invocation(tmp_path, capsys):
    """The dispatcher only resolves: anything else is a usage error.

    The old exec form (``dispatcher <command>``) is gone with the exec
    path; the shim always calls ``dispatcher resolve <command>``.
    Production call site: ``_dispatch_runtime.main``.
    """
    dispatcher = tmp_path / "dispatcher"
    dispatcher.write_text("#!/bin/sh\n", encoding="utf-8")
    for argv in (
        [str(dispatcher)],
        [str(dispatcher), "somecmd"],
        [str(dispatcher), "resolve"],
        [str(dispatcher), "resolve", ""],
        [str(dispatcher), "resolve", "a", "extra"],
        [str(dispatcher), "exec", "somecmd"],
    ):
        code = _dispatch_runtime.main(argv)
        captured = capsys.readouterr()
        assert code == 2, argv
        assert "usage: dispatcher resolve <command>" in captured.err, argv
        assert captured.out == "", argv


def test_r12_reset_publishes_empty_and_permits_reinstall(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """R12: reset publishes an empty generation; reinstalling then works.

    Source: full-a ``test_r12_reset_actually_permits_reinstall`` (and
    the ``recovery-reset-not-publishable`` verdict finding). Corrupt
    generations are removed, the fresh empty registry stays active,
    and the advised reinstall publishes again. Production call site:
    ``cli.main(["dispatch", "recover", "--reset"])`` ->
    ``dispatch.recover_registry``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(
        skills_root, "skill-reset", {"resetable": "RESET-OK"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-reset", "tag": "v1"}]
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    for child in dispatch.generations_dir(csk_home).iterdir():
        (child / "registry.json").write_text("{broken")
    assert cli.main(["dispatch", "recover", "--reset"]) == 0
    assert "reset" in capsys.readouterr().out
    registry, empty_generation = _read_registry(csk_home)
    assert registry["projects"] == {}
    assert registry["global"]["commands"] == {}
    remaining = [
        child.name for child in dispatch.generations_dir(csk_home).iterdir()
    ]
    assert remaining == [empty_generation]
    results = installer.install(cfg)
    assert not results[0].errors, results[0].errors
    proc = _bare("resetable", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("RESET-OK")


def test_r12_reset_permits_reinstall_after_non_utf8_state(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """R12: reset heals non-UTF8 pointer and generation bytes.

    Source: full-b ``test_panel_reset_allows_reinstall``. Same class
    as the JSON-corruption reset test, through the undecodable-bytes
    taxonomy path. Production call site: ``dispatch.recover_registry``
    with ``reset=True``.
    """
    project = make_project(tmp_path, "reset-app")
    _make_command_skill(
        skills_root, "panel-reset-skill", {"panelreset": "RESET-OK"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "panel-reset-skill", "tag": "v1"}]
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    for child in dispatch.generations_dir(csk_home).iterdir():
        (child / "registry.json").write_bytes(b"\xff\xfe\x00bad")
    dispatch.current_path(csk_home).write_bytes(b"\xff\xfe\x00bad")
    with monkeypatch.context() as context:
        context.setattr(dispatch, "_fault_hook", None)
        assert cli.main(["dispatch", "recover", "--reset"]) == 0
    results = installer.install(cfg)
    assert not results[0].errors, results[0].errors
    proc = _bare("panelreset", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert "RESET-OK" in proc.stdout


def test_r12_reset_removes_all_corrupt_generations(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """R12: every corrupt generation is gone after reset, reinstall works.

    Source: delta ``test_reviewer_reset_can_republish_after_all_generations_corrupt``.
    Production call site: ``dispatch.recover_registry`` with
    ``reset=True``.
    """
    project = make_project(tmp_path, "proj")
    _make_command_skill(
        skills_root, "skill-reset", {"resetable": "RESET"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-reset", "tag": "v1"}]
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    for child in dispatch.generations_dir(csk_home).iterdir():
        (child / "registry.json").write_bytes(b"\xff")
    assert cli.main(["dispatch", "recover", "--reset"]) == 0
    capsys.readouterr()
    remaining = list(dispatch.generations_dir(csk_home).iterdir())
    assert len(remaining) == 1
    results = installer.install(cfg)
    assert not results[0].errors, results[0].errors
    proc = _bare("resetable", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("RESET")


def _install_recovery_pair(csk_home, skills_root, project):
    """Install v1 (oldcmd) then v2 (newcmd); return the active gen id."""
    _make_command_skill(skills_root, "rskill", {"oldcmd": "V1"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "rskill", "tag": "v1"}])
    _make_command_skill(skills_root, "rskill", {"newcmd": "V2"}, tag="v2")
    _install_project(csk_home, skills_root, project, [{"name": "rskill", "tag": "v2"}])
    _registry, generation = _read_registry(csk_home)
    return generation


def test_r12_recovery_stages_before_pointer_and_retries_converge(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """R12: recovery stages launchers before switching; retries converge.

    Sources: full-a ``test_r12_recovery_interruption_retries_missing_launchers``
    and delta ``test_reviewer_explicit_recovery_stages_before_pointer``
    (the ``recovery-pointer-before-staging`` verdict finding). An
    interrupted stage leaves the pointer on the corrupt generation
    (asserted byte-identical); the retry activates the older
    generation and restores its launchers. The recovered v1 pin's
    target was pruned by the v2 install, so the restored launcher
    honestly reports the broken pin instead of serving it. Production
    call site: ``dispatch.recover_registry``.
    """
    project = make_project(tmp_path, "proj")
    generation = _install_recovery_pair(csk_home, skills_root, project)
    (dispatch.generations_dir(csk_home) / generation / "registry.json").write_text(
        "{broken"
    )
    (dispatch.shims_dir(csk_home) / "oldcmd").unlink(missing_ok=True)
    attempts = {"count": 0}
    real_stage = dispatch.stage_launchers

    def fail_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("injected stage failure")
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(dispatch, "stage_launchers", fail_once)
    before = dispatch.current_path(csk_home).read_bytes()
    assert before == (generation + "\n").encode("utf-8")
    with pytest.raises(RuntimeError, match="injected stage failure"):
        dispatch.recover_registry(csk_home)
    after = dispatch.current_path(csk_home).read_bytes()
    assert after == before
    monkeypatch.setattr(dispatch, "stage_launchers", real_stage)
    message = dispatch.recover_registry(csk_home)
    assert "recovered" in message
    assert (dispatch.shims_dir(csk_home) / "oldcmd").exists()
    # The launcher is back; the pruned v1 target reports honestly.
    proc = _bare("oldcmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert "reinstall" in proc.stderr.lower()
    proc = _bare("newcmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 127, (proc.returncode, proc.stderr)
    capsys.readouterr()


def test_r12_recovery_swap_fault_retries_converge(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """R12: a failed pointer switch retries to the recovered generation.

    Source: full-b ``test_panel_recovery_stages_old_exports_before_pointer``
    (adapted: rev4 shares one activation function, so the fault lands
    on its switch step instead of the setup-path staging call). The
    staged launchers survive the fault, the pointer stays on the
    corrupt generation, and the retry converges. Production call site:
    ``dispatch.recover_registry`` -> ``_activate_generation``.
    """
    project = make_project(tmp_path, "panel-app")
    generation = _install_recovery_pair(csk_home, skills_root, project)
    (dispatch.generations_dir(csk_home) / generation / "registry.json").write_text(
        "{broken"
    )
    real_swap = dispatch._swap_current_pointer
    attempts = {"count": 0}

    def fail_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("injected swap failure")
        return real_swap(*args, **kwargs)

    monkeypatch.setattr(dispatch, "_swap_current_pointer", fail_once)
    before = dispatch.current_path(csk_home).read_bytes()
    assert before == (generation + "\n").encode("utf-8")
    with pytest.raises(RuntimeError, match="injected swap failure"):
        dispatch.recover_registry(csk_home)
    assert dispatch.current_path(csk_home).read_bytes() == before
    monkeypatch.setattr(dispatch, "_swap_current_pointer", real_swap)
    message = dispatch.recover_registry(csk_home)
    assert "recovered" in message
    assert (dispatch.shims_dir(csk_home) / "oldcmd").exists()


def test_r8_registry_symlink_into_checkout_refuses(
    tmp_path, skills_root, csk_home
):
    """R8: a registry reached through a symlink refuses before reading.

    Source: full-a ``test_r8_registry_symlink_into_checkout_refuses``
    (the ``registry-symlink-checkout-read`` verdict finding), extended
    to the pointer and the generations directory: every component
    along the registry path is verified non-symlink before any
    authoritative byte is read. Production call site: bare shim ->
    ``_dispatch_runtime.load_registry``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(
        skills_root, "skill-sym", {"symcmd": "SYM-OK"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-sym", "tag": "v1"}]
    )
    checkout = project / "checkout"
    checkout.mkdir()
    (checkout / "registry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": {},
                "global": {"skills": [], "commands": {}},
                "shim_case_insensitive": False,
            }
        ),
        encoding="utf-8",
    )
    dispatch_dir = dispatch.dispatch_dir(csk_home)
    _registry, generation = _read_registry(csk_home)
    registry_path = dispatch.generations_dir(csk_home) / generation / "registry.json"
    saved = registry_path.read_bytes()

    def restore() -> None:
        current = dispatch_dir / "current"
        if current.is_symlink():
            current.unlink()
        generations = dispatch.generations_dir(csk_home)
        if generations.is_symlink():
            generations.unlink()
            generations.mkdir()
            (generations / generation).mkdir()
        registry_path.unlink(missing_ok=True)
        registry_path.write_bytes(saved)

    try:
        # Shape 1: the registry file itself is a checkout alias.
        registry_path.unlink()
        registry_path.symlink_to(checkout / "registry.json")
        proc = _bare("symcmd", cwd=project, csk_home=csk_home)
        assert proc.returncode != 0, (proc.returncode, proc.stdout)
        assert "symlink" in proc.stderr, proc.stderr
        assert "SYM-OK" not in proc.stdout
        restore()
        # Shape 2: the current pointer is a checkout alias.
        current = dispatch_dir / "current"
        pointer_saved = current.read_bytes()
        current.unlink()
        current.symlink_to(checkout / "registry.json")
        proc = _bare("symcmd", cwd=project, csk_home=csk_home)
        assert proc.returncode != 0, (proc.returncode, proc.stdout)
        assert "symlink" in proc.stderr, proc.stderr
        current.unlink()
        current.write_bytes(pointer_saved)
        # Shape 3: the generations directory is a checkout alias.
        generations = dispatch.generations_dir(csk_home)
        generations_target = tmp_path / "generations-real"
        generations.rename(generations_target)
        try:
            generations.symlink_to(checkout)
            proc = _bare("symcmd", cwd=project, csk_home=csk_home)
            assert proc.returncode != 0, (proc.returncode, proc.stdout)
            assert "symlink" in proc.stderr, proc.stderr
        finally:
            generations.unlink()
            generations_target.rename(generations)
    finally:
        restore()
    # The manager heals once the aliases are gone.
    proc = _bare("symcmd", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("SYM-OK")


def test_r5_status_check_rejects_missing_published_pointer(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """R5: status reports a missing published pointer as failure.

    Source: full-a ``test_r5_status_check_rejects_missing_published_pointer``
    (the ``missing-current-registry-as-absence`` verdict finding): a
    known publication read failure stays a failure in ``status
    --check``, never absence. Production call site:
    ``dispatch.verify_project_digests``.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(
        skills_root, "skill-status", {"stcmd": "STATUS-OK"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-status", "tag": "v1"}]
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    assert cli.main(["status", "app", "--check"]) == 0
    capsys.readouterr()
    dispatch.current_path(csk_home).unlink()
    assert cli.main(["status", "app", "--check"]) == 1
    captured = capsys.readouterr()
    assert "cannot be verified" in captured.err
    assert "missing" in captured.err


def test_r4_permission_denied_record_refuses_without_guessing(
    tmp_path, monkeypatch
):
    """R4 control: an unverifiable deepest record refuses (held).

    Source: full-a ``test_r4_permission_denied_ancestor_refuses_unknown``.
    A registered root the matcher cannot stat fails closed instead of
    guessing; records outside the CWD path are never even stated.
    Production call site: ``_dispatch_runtime.find_project``.
    """
    root = tmp_path / "proj"
    root.mkdir()
    real_stat = os.stat
    projects = {
        "f" * 64: {
            "checkout_id": "f" * 64,
            "canonical_root": str(root),
            "root_identity": {"st_ino": str(root.stat().st_ino)},
        }
    }

    def denied_stat(path, *args, **kwargs):
        if os.fspath(path) == str(root):
            raise PermissionError("injected permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(_dispatch_runtime.os, "stat", denied_stat)
    with pytest.raises(DispatchError, match="refusing to guess"):
        _dispatch_runtime.find_project(
            projects, str(root / "sub"), manager_home=str(tmp_path)
        )


def test_r4_permission_denied_record_refuses_in_memory(
    tmp_path, monkeypatch
):
    """R4 control: the in-memory permission-denied shape refuses (held).

    Source: full-b ``test_panel_permission_denied_ancestor_refuses``.
    Production call site: ``_dispatch_runtime.find_project``.
    """
    root = tmp_path / "panel-app"
    root.mkdir()
    real_stat = os.stat
    projects = {
        "f" * 64: {
            "checkout_id": "f" * 64,
            "canonical_root": str(root),
            "root_identity": {"st_ino": str(root.stat().st_ino)},
        }
    }

    def denied_stat(path, *args, **kwargs):
        if os.fspath(path) == str(root):
            raise PermissionError("injected EACCES")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(_dispatch_runtime.os, "stat", denied_stat)
    with pytest.raises(DispatchError, match="refusing to guess"):
        _dispatch_runtime.find_project(
            projects, str(root), manager_home=str(tmp_path)
        )


def test_r4_cross_volume_inode_coincidence_ignored(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """R4: an inode coincidence without a path match never selects.

    Source: full-a ``test_r4_cross_volume_inode_collision_does_not_select_other_root``
    (the ``inode-only-alias-selects-unrelated-root`` verdict finding),
    adapted to RESOLVE mode: the old execve interception is gone, so
    selection is observed through the resolve protocol. The stat view
    reports the project inode for the unrelated directory (the
    simulated cross-volume alias); resolve still reports unavailable
    (127), never the project pin and never a moved-root veto.
    Production call site: ``_dispatch_runtime.main`` resolve mode.
    """
    project = make_project(tmp_path, "proj-p")
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_command_skill(
        skills_root, "skill-cross", {"crossA": "PROJECT-A"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-cross", "tag": "v1"}]
    )
    registry, _generation = _read_registry(csk_home)
    project_ino = int(
        next(iter(registry["projects"].values()))["root_identity"]["st_ino"]
    )
    real_stat = os.stat

    class _AliasedStat:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            if name == "st_ino":
                return project_ino
            return getattr(self._wrapped, name)

    def aliased_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if os.fspath(path) == str(outside):
            return _AliasedStat(info)
        return info

    monkeypatch.setattr(_dispatch_runtime.os, "stat", aliased_stat)
    here = os.getcwd()
    os.chdir(outside)
    try:
        code, out, err = _resolve_in_process(csk_home, "crossA", capsys)
    finally:
        os.chdir(here)
    assert code == 127, (code, out, err)
    assert "outside every registered project" in err
    assert out == ""


def test_r4_unrelated_volume_inode_ignored(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """R4: a same-inode unrelated volume reports outside, never selects.

    Source: full-b ``test_panel_unrelated_volume_inode_does_not_select_project``,
    adapted to RESOLVE mode (and made non-vacuous: the aliased path is
    a real directory, so the stat view genuinely reports the project
    inode for it). Production call site: ``_dispatch_runtime.main``
    resolve mode.
    """
    project = make_project(tmp_path, "panel-app")
    outside = tmp_path / "panel-outside"
    outside.mkdir()
    _make_command_skill(
        skills_root, "panel-iso-skill", {"paneliso": "PROJECT"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "panel-iso-skill", "tag": "v1"}]
    )
    registry, _generation = _read_registry(csk_home)
    project_ino = int(
        next(iter(registry["projects"].values()))["root_identity"]["st_ino"]
    )
    real_stat = os.stat

    class _AliasedStat:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            if name == "st_ino":
                return project_ino
            return getattr(self._wrapped, name)

    def aliased_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if os.fspath(path) == str(outside):
            assert info.st_ino != project_ino
            return _AliasedStat(info)
        return info

    monkeypatch.setattr(_dispatch_runtime.os, "stat", aliased_stat)
    monkeypatch.setattr(_dispatch_runtime.os, "getcwd", lambda: str(outside))
    code, out, err = _resolve_in_process(csk_home, "paneliso", capsys)
    assert code == 127, (code, out, err)
    assert "outside every" in err
    assert out == ""


def test_r4_unstatable_shallower_record_never_vetoes_deeper(
    tmp_path, monkeypatch
):
    """R4: an unverifiable enclosing record cannot veto the live scope.

    Only the deepest path-matching records are verified: an outer
    record that cannot be stated is ignored when a live inner record
    selects the CWD. Production call site:
    ``_dispatch_runtime.find_project``.
    """
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    real_stat = os.stat
    projects = {
        "a" * 64: {
            "checkout_id": "a" * 64,
            "canonical_root": str(outer),
            "root_identity": {"st_ino": str(outer.stat().st_ino)},
        },
        "b" * 64: {
            "checkout_id": "b" * 64,
            "canonical_root": str(inner),
            "root_identity": {"st_ino": str(inner.stat().st_ino)},
        },
    }

    def denied_outer(path, *args, **kwargs):
        if os.fspath(path) == str(outer):
            raise PermissionError("injected EACCES")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(_dispatch_runtime.os, "stat", denied_outer)
    found = _dispatch_runtime.find_project(
        projects, str(inner / "sub"), manager_home=str(tmp_path)
    )
    assert found is not None
    assert found["checkout_id"] == "b" * 64


def _union_entry(root: str, commands: dict[str, str]) -> dict:
    """Build a minimal project entry for union-validation tests."""
    return {
        "canonical_root": root,
        "commands": {
            name: {"owner": owner} for name, owner in commands.items()
        },
    }


def test_public_shim_union_validation(tmp_path):
    """The public shim namespace refuses case-only collisions anywhere.

    One spelling shared by several scopes is one shim resolved per
    directory, so it publishes; two spellings differing only by case
    cannot share a file and refuse with both owners named. Stale
    records never veto the union. This pure-function test is the
    volume-independent killer for the union-narrowing mutant.
    Production call site: ``dispatch.validate_public_shim_union``.
    """
    live_p = tmp_path / "p"
    live_p.mkdir()
    live_q = tmp_path / "q"
    live_q.mkdir()
    projects = {
        "a" * 64: {
            **_union_entry(str(live_p), {"tool": "skill-a"}),
            "root_identity": {"st_ino": str(live_p.stat().st_ino)},
        },
        "b" * 64: {
            **_union_entry(str(live_q), {"tool": "skill-b"}),
            "root_identity": {"st_ino": str(live_q.stat().st_ino)},
        },
    }
    # Same spelling across scopes: fine under both rules.
    dispatch.validate_public_shim_union(
        projects=projects, global_commands={}, case_insensitive=True
    )
    dispatch.validate_public_shim_union(
        projects=projects, global_commands={}, case_insensitive=False
    )
    # Case-only collision across projects refuses, both owners named.
    clashing = {
        "a" * 64: {
            **_union_entry(str(live_p), {"Casepinpanel": "skill-a"}),
            "root_identity": {"st_ino": str(live_p.stat().st_ino)},
        },
        "b" * 64: {
            **_union_entry(str(live_q), {"casepinpanel": "skill-b"}),
            "root_identity": {"st_ino": str(live_q.stat().st_ino)},
        },
    }
    with pytest.raises(dispatch.DispatchPublishError) as excinfo:
        dispatch.validate_public_shim_union(
            projects=clashing, global_commands={}, case_insensitive=True
        )
    message = str(excinfo.value)
    assert "skill-a" in message and "skill-b" in message, message
    assert str(live_p) in message and str(live_q) in message, message
    # The same pair publishes on a case-sensitive shims volume.
    dispatch.validate_public_shim_union(
        projects=clashing, global_commands={}, case_insensitive=False
    )
    # Case-only collision between a project and global refuses too.
    with pytest.raises(dispatch.DispatchPublishError) as excinfo:
        dispatch.validate_public_shim_union(
            projects={
                "a" * 64: {
                    **_union_entry(str(live_p), {"GTool": "skill-a"}),
                    "root_identity": {
                        "st_ino": str(live_p.stat().st_ino)
                    },
                }
            },
            global_commands={"gtool": {"owner": "skill-g"}},
            case_insensitive=True,
        )
    message = str(excinfo.value)
    assert "skill-a" in message and "skill-g" in message, message
    assert "global" in message, message
    # A stale record's names never veto a live publication.
    stale = {
        "a" * 64: {
            **_union_entry(str(live_p), {"Tool": "skill-a"}),
            "root_identity": {"st_ino": str(live_p.stat().st_ino)},
        },
        "dead": {
            **_union_entry(
                str(tmp_path / "gone"), {"tool": "skill-dead"}
            ),
            "root_identity": {"st_ino": "1"},
        },
    }
    dispatch.validate_public_shim_union(
        projects=stale, global_commands={}, case_insensitive=True
    )


def test_r9_cross_project_case_collision_refuses_naming_both_owners(
    tmp_path, skills_root, csk_home
):
    """R9: a cross-project case-only collision refuses (or both serve).

    Source: full-a ``test_r9_two_projects_case_alias_exports_remain_callable``
    (the ``cross-layer-command-owner-collision`` verdict finding,
    repeat of revision 2). On a case-insensitive shims volume the
    second install refuses naming both owners and the first pin keeps
    serving; on a case-sensitive volume both publish and both serve.
    Production call site: ``installer.install`` ->
    ``dispatch.publish_project`` -> ``validate_public_shim_union``.
    """
    first = make_project(tmp_path, "casepanel-p")
    second = make_project(tmp_path, "casepanel-q")
    _make_command_skill(
        skills_root, "casepin-a", {"Casepinpanel": "P-ONE"}, tag="v1"
    )
    _make_command_skill(
        skills_root, "casepin-b", {"casepinpanel": "Q-TWO"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, first, [{"name": "casepin-a", "tag": "v1"}]
    )
    proc = _bare("Casepinpanel", cwd=first, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("P-ONE")
    if not dispatch.shims_case_insensitive(csk_home):
        _install_project(
            csk_home, skills_root, second, [{"name": "casepin-b", "tag": "v1"}]
        )
        for cwd, name, marker in (
            (first, "Casepinpanel", "P-ONE"),
            (second, "casepinpanel", "Q-TWO"),
        ):
            proc = _bare(name, cwd=cwd, csk_home=csk_home)
            assert proc.returncode == 0, (name, proc.stderr)
            assert proc.stdout.startswith(marker)
        return
    results = _install_project_allowing_errors(
        csk_home, skills_root, second, [{"name": "casepin-b", "tag": "v1"}]
    )
    assert results[0].errors, "the colliding install must refuse"
    assert "casepin-a" in results[0].errors[0], results[0].errors
    assert "casepin-b" in results[0].errors[0], results[0].errors
    proc = _bare("Casepinpanel", cwd=first, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("P-ONE")


def test_panel_case_collision_across_projects(tmp_path, skills_root, csk_home):
    """R9: cross-project case variants refuse safely or both serve.

    Source: full-b ``test_panel_case_collision_across_projects``
    verbatim. Either the second install refuses (case-insensitive
    shims volume) with the first pin intact, or both install and both
    serve (case-sensitive volume). Production call site:
    ``installer.install`` -> ``dispatch.publish_project``.
    """
    first = make_project(tmp_path, "panel-p")
    second = make_project(tmp_path, "panel-q")
    _make_command_skill(skills_root, "case-a", {"PanelCase": "P-ONE"}, tag="v1")
    _make_command_skill(skills_root, "case-b", {"panelcase": "Q-TWO"}, tag="v1")
    _install_project(csk_home, skills_root, first, [{"name": "case-a", "tag": "v1"}])
    results = _install_project_allowing_errors(
        csk_home, skills_root, second, [{"name": "case-b", "tag": "v1"}]
    )
    if results[0].errors:
        proc = _bare("PanelCase", cwd=first, csk_home=csk_home)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("P-ONE")
        return
    for cwd, name, marker in (
        (first, "PanelCase", "P-ONE"),
        (second, "panelcase", "Q-TWO"),
    ):
        proc = _bare(name, cwd=cwd, csk_home=csk_home)
        assert proc.returncode == 0, (name, proc.stderr)
        assert proc.stdout.startswith(marker), (name, proc.stdout)


def test_r9_global_only_case_collision_refuses(tmp_path, skills_root, csk_home):
    """R9 control: a global-only case collision refuses (held).

    Source: full-a ``test_r9_global_only_case_collision_refuses``.
    Production call site: ``global_install.install`` closure/collision
    planning.
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "gcase-a", {"Casepanel": "A"}, tag="v1")
    _make_command_skill(skills_root, "gcase-b", {"casepanel": "B"}, tag="v1")
    results = _install_global_allowing_errors(
        csk_home,
        skills_root,
        project,
        [{"name": "gcase-a", "tag": "v1"}, {"name": "gcase-b", "tag": "v1"}],
    )
    assert results.errors, "global-only case collision must refuse"


def test_r11_ignored_sigpipe_survives_in_target(
    tmp_path, skills_root, csk_home, monkeypatch
):
    """R11: a caller-ignored SIGPIPE stays ignored in the target.

    Source: full-a ``test_r11_preserve_caller_ignored_sigpipe`` (the
    ``exec-sigpipe-inheritance`` verdict finding). Production call
    site: bare shim -> ``exec "$target"``.
    """
    project = make_project(tmp_path, "proj-p")
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "sigpipe": {
                        "type": "script",
                        "unix_path": "scripts/sigpipe",
                    }
                },
            }
        ),
        "scripts/sigpipe": "#!/bin/sh\nkill -PIPE $$\nprintf 'SURVIVED\\n'\n",
    }
    make_skill_repo(skills_root, "pipe-skill", files, tag="v1")
    _install_project(
        csk_home, skills_root, project, [{"name": "pipe-skill", "tag": "v1"}]
    )
    shims = str(dispatch.shims_dir(csk_home))

    original_popen = subprocess.Popen

    def popen_with_ignored_sigpipe(*args, **kwargs):
        kwargs["preexec_fn"] = lambda: signal.signal(
            signal.SIGPIPE, signal.SIG_IGN
        )
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen_with_ignored_sigpipe)
    env = dict(os.environ)
    env["PATH"] = env["PATH"] + os.pathsep + shims
    try:
        proc = subprocess.run(
            ["sh", "-c", 'exec "$@"', "sh", "sigpipe"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
        )
    finally:
        monkeypatch.setattr(subprocess, "Popen", original_popen)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.startswith("SURVIVED")


def test_r11_ignored_sigpipe_preserved_via_preexec(
    tmp_path, skills_root, csk_home
):
    """R11: ignored SIGPIPE survives, via preexec-set disposition.

    Source: full-b ``test_panel_ignored_sigpipe_is_preserved``
    verbatim. Production call site: bare shim -> ``exec "$target"``.
    """
    project = make_project(tmp_path, "panel-app")
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "sigpipe": {
                        "type": "script",
                        "unix_path": "scripts/sigpipe",
                    }
                },
            }
        ),
        "scripts/sigpipe": "#!/bin/sh\nkill -PIPE $$\nprintf 'SURVIVED\\n'\n",
    }
    make_skill_repo(skills_root, "panel-pipe-skill", files, tag="v1")
    _install_project(
        csk_home, skills_root, project, [{"name": "panel-pipe-skill", "tag": "v1"}]
    )
    env = dict(os.environ)
    env["PATH"] = env["PATH"] + os.pathsep + str(dispatch.shims_dir(csk_home))

    def ignore_sigpipe() -> None:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    proc = subprocess.run(
        ["sh", "-c", 'exec "$@"', "sh", "sigpipe"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        preexec_fn=ignore_sigpipe,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.startswith("SURVIVED")


def test_r11_caller_csk_vars_preserved(tmp_path, skills_root, csk_home):
    """R11: caller-owned _CSK_* variables reach the target intact.

    Source: full-b ``test_panel_wrapper_preserves_non_sentinel_env``
    (the ``caller-environment-clobbered`` verdict finding): the
    wrapper exports nothing, so caller bookkeeping keys survive.
    Production call site: bare shim -> ``exec "$target"``.
    """
    project = make_project(tmp_path, "panel-app")
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "showenv": {
                        "type": "script",
                        "unix_path": "scripts/showenv",
                    }
                },
            }
        ),
        "scripts/showenv": (
            "#!/bin/sh\n"
            "printf 'PY:%s\\n' \"${_CSK_PYTHON-unset}\"\n"
            "printf 'RT:%s\\n' \"${_CSK_RUNTIME-unset}\"\n"
        ),
    }
    make_skill_repo(skills_root, "panel-env-skill", files, tag="v1")
    _install_project(
        csk_home, skills_root, project, [{"name": "panel-env-skill", "tag": "v1"}]
    )
    env = dict(os.environ)
    env["PATH"] = env["PATH"] + os.pathsep + str(dispatch.shims_dir(csk_home))
    env["_CSK_PYTHON"] = "caller-python"
    env["_CSK_RUNTIME"] = "caller-runtime"
    proc = subprocess.run(
        ["sh", "-c", 'exec "$@"', "sh", "showenv"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert "PY:caller-python" in proc.stdout, proc.stdout
    assert "RT:caller-runtime" in proc.stdout, proc.stdout


def test_r11_caller_environment_sentinel_preserved(
    tmp_path, skills_root, csk_home
):
    """R11: a caller _CSK_ENV_* value is never clobbered or dropped.

    Source: delta ``test_reviewer_caller_environment_sentinel_is_preserved``.
    Production call site: bare shim -> ``exec "$target"``.
    """
    project = make_project(tmp_path, "proj")
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "showenv": {
                        "type": "script",
                        "unix_path": "scripts/showenv",
                    }
                },
            }
        ),
        "scripts/showenv": (
            "#!/bin/sh\nprintf 'VAL:%s\\n' \"${_CSK_ENV_VAL_LC_CTYPE-unset}\"\n"
        ),
    }
    make_skill_repo(skills_root, "skill-show", files, tag="v1")
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-show", "tag": "v1"}]
    )
    env = dict(os.environ)
    env["PATH"] = env["PATH"] + os.pathsep + str(dispatch.shims_dir(csk_home))
    env["_CSK_ENV_VAL_LC_CTYPE"] = "caller-owned"
    proc = subprocess.run(
        ["sh", "-c", 'exec "$@"', "sh", "showenv"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert "VAL:caller-owned" in proc.stdout, proc.stdout


def test_shim_lowercase_reservation_scrubs_caller_collision(
    tmp_path, skills_root, csk_home
):
    """The shim's lowercase _csk_* namespace is reserved and scrubbed.

    A caller exporting the shim's working names (``_csk_target``,
    ``_csk_out``) must see neither its own values nor the shim's
    resolved values in the target: the reservation unsets the whole
    namespace before exec, so no dispatcher-private value can leak
    through an inherited export attribute. Uppercase
    ``_CSK_DISPATCHER``/``_CSK_COMMAND`` stay caller-preserved (see the
    generated transparency test); only the lowercase working namespace
    is reserved. Single-case killer for the scrub narrowing mutant.
    Production call site: ``dispatch.shim_text``.
    """
    project = make_project(tmp_path, "proj")
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "showvars": {
                        "type": "script",
                        "unix_path": "scripts/showvars",
                    }
                },
            }
        ),
        "scripts/showvars": (
            "#!/bin/sh\n"
            "printf 'TARGET:%s\\n' \"${_csk_target-unset}\"\n"
            "printf 'OUT:%s\\n' \"${_csk_out-unset}\"\n"
            "printf 'CONTROL:%s\\n' \"${CALLER_CONTROL-unset}\"\n"
        ),
    }
    make_skill_repo(skills_root, "skill-showvars", files, tag="v1")
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-showvars", "tag": "v1"}]
    )
    env = dict(os.environ)
    env["PATH"] = env["PATH"] + os.pathsep + str(dispatch.shims_dir(csk_home))
    env["_csk_target"] = "caller-target"
    env["_csk_out"] = "caller-out"
    env["CALLER_CONTROL"] = "caller-control"
    proc = subprocess.run(
        ["sh", "-c", 'exec "$@"', "sh", "showvars"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert "TARGET:unset" in proc.stdout, proc.stdout
    assert "OUT:unset" in proc.stdout, proc.stdout
    assert "CONTROL:caller-control" in proc.stdout, proc.stdout


def test_r6_metadata_preserving_hardlink_is_declared_bound(
    tmp_path, skills_root, csk_home
):
    """R6 control: the hardlink metadata bound holds (held).

    Source: full-a ``test_r6_metadata_preserving_hardlink_is_declared_bound``.
    A metadata-preserving content swap through a second link stays
    invisible per call and is caught by ``status --check``.
    Production call sites: bare shim (per-call tuple) and
    ``dispatch.verify_project_digests`` (full sha256).
    """
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-hl", {"hltool": "HL-OK"}, tag="v1")
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-hl", "tag": "v1"}]
    )
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    target = Path(entry["commands"]["hltool"]["target"])
    digest = entry["commands"]["hltool"]["digest"]
    original = target.read_bytes()
    replacement = original.replace(b"HL-OK", b"HL-XX")
    assert replacement != original and len(replacement) == len(original)
    spare = tmp_path / "spare"
    spare.write_bytes(replacement)
    os.unlink(target)
    os.link(spare, target)
    info = target.stat()
    os.utime(target, ns=(info.st_atime_ns, int(digest["mtime_ns"])))
    proc = _bare("hltool", cwd=project, csk_home=csk_home)
    if str(info.st_ino) == digest["st_ino"]:
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines()[0] == "HL-XX"
    problems = dispatch.verify_project_digests(
        csk_home, [entry["canonical_root"]]
    )
    if str(info.st_ino) == digest["st_ino"]:
        assert any("sha256" in problem for problem in problems), problems
    else:
        assert any("no longer matches" in problem for problem in problems), (
            problems
        )


def test_panel_repo_first_path_cannot_steer_shim(
    tmp_path, skills_root, csk_home
):
    """R3 control: a repo-first PATH cannot steer the selected shim (held).

    Source: full-b ``test_panel_repo_first_path_cannot_steer_selected_shim``.
    The shim resolves through the absolute manager dispatcher, never
    through caller PATH entries. Production call site: the generated
    shim's absolute ``_CSK_DISPATCHER``.
    """
    project = make_project(tmp_path, "panel-app")
    _make_command_skill(
        skills_root, "panel-steer-skill", {"panelsteer": "MANAGER"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "panel-steer-skill", "tag": "v1"}]
    )
    decoy_dir = project / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "panelsteer"
    decoy.write_text('#!/bin/sh\necho "DECOY"\n', encoding="utf-8")
    decoy.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = (
        str(decoy_dir) + os.pathsep + env["PATH"] + os.pathsep + str(dispatch.shims_dir(csk_home))
    )
    proc = subprocess.run(
        [str(dispatch.shims_dir(csk_home) / "panelsteer")],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.startswith("MANAGER"), proc.stdout


@pytest.mark.parametrize(
    "operation", ["fsync-fails", "chmod-fails", "replace-fails"]
)
@pytest.mark.parametrize("position", ["before", "after"])
def test_atomic_writer_before_after_faults_keep_old_or_new(
    tmp_path, monkeypatch, operation, position
):
    """The atomic writer fails old-or-new at every syscall step.

    Source: delta ``test_reviewer_atomic_writer_failure_steps_old_or_new``.
    Failing fsync/chmod/replace, before or after the live file is
    staged, keeps the old bytes (or the fully committed new bytes) and
    no temp file behind. Production call site:
    ``dispatch.atomic_write_bytes``.
    """
    from csk import dispatch as dispatch_module

    target = tmp_path / "live"
    target.write_text("OLD", encoding="utf-8")
    real_fsync = os.fsync
    real_chmod = os.chmod
    real_replace = os.replace

    def failing_fsync(*args, **kwargs):
        if position == "before":
            raise OSError("injected fsync failure")
        return real_fsync(*args, **kwargs)

    def failing_chmod(*args, **kwargs):
        if position == "before":
            raise OSError("injected chmod failure")
        return real_chmod(*args, **kwargs)

    def failing_replace(src, dst, *args, **kwargs):
        if position == "before":
            raise OSError("injected replace failure")
        return real_replace(src, dst, *args, **kwargs)

    if operation == "fsync-fails":
        monkeypatch.setattr(dispatch_module.os, "fsync", failing_fsync)
    elif operation == "chmod-fails":
        monkeypatch.setattr(dispatch_module.os, "chmod", failing_chmod)
    else:
        monkeypatch.setattr(dispatch_module.os, "replace", failing_replace)
    if position == "before":
        with pytest.raises(OSError, match="injected"):
            dispatch_module.atomic_write_bytes(target, b"NEW", mode=0o644)
        assert target.read_text(encoding="utf-8") == "OLD"
    else:
        dispatch_module.atomic_write_bytes(target, b"NEW", mode=0o644)
        assert target.read_text(encoding="utf-8") == "NEW"
    leftovers = [
        child
        for child in tmp_path.iterdir()
        if child.name != "live" and ".live." in child.name
    ]
    assert leftovers == []


def test_atomic_writer_failure_removes_temp_file(tmp_path, monkeypatch):
    """A failed atomic write leaves no staged temp file behind.

    Single-case killer for the temp-cleanup narrowing mutant: the
    replace step fails, the error propagates, the live bytes stay old,
    and the staged temp file is removed. Production call site:
    ``dispatch.atomic_write_bytes``.
    """
    from csk import dispatch as dispatch_module

    target = tmp_path / "live"
    target.write_text("OLD", encoding="utf-8")

    def failing_replace(src, dst, *args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(dispatch_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        dispatch_module.atomic_write_bytes(target, b"NEW", mode=0o644)
    assert target.read_text(encoding="utf-8") == "OLD"
    assert [child.name for child in tmp_path.iterdir()] == ["live"]


def test_d7_pre_upgrade_project_refuses_then_migrates_on_reinstall(
    tmp_path, skills_root, csk_home, monkeypatch, capsys
):
    """D7: a pre-dispatch install refuses explicitly, then migrates.

    Source: delta ``test_reviewer_rev2_pre_upgrade_reproduction_now_passes``
    (the ``pre-upgrade-project-record-missing`` verdict finding), closed
    via the verdict's allowed ``explicit migration/refusal`` branch: a
    project installed before dispatch records existed carries no
    guessable pin, so calls inside it refuse with reinstall guidance
    instead of silently serving the global fallback, and reinstalling
    publishes its record and serves its pin. Production call sites:
    ``dispatch.publish_legacy_scopes`` (tombstone), bare shim ->
    ``_dispatch_runtime.select_command`` (refusal), and
    ``installer.install`` (migration).
    """
    project = make_project(tmp_path, "legacy")
    _make_command_skill(
        skills_root, "delta-legacy", {"deltalegacycmd": "LEGACY-PROJECT"}, tag="v1"
    )
    with monkeypatch.context() as context:
        context.setattr(dispatch, "publish_supported", lambda: False)
        _install_project(
            csk_home, skills_root, project, [{"name": "delta-legacy", "tag": "v1"}]
        )
    repo, _commit = _make_command_skill(
        skills_root, "delta-legacy", {"deltalegacycmd": "GLOBAL"}, tag="v2"
    )
    assert repo is not None
    _install_global(
        csk_home, skills_root, project, [{"name": "delta-legacy", "tag": "v2"}]
    )
    registry, _generation = _read_registry(csk_home)
    entries = list(registry["projects"].values())
    assert len(entries) == 1
    assert entries[0]["needs_migration"] is True

    # Explicit refusal inside the unmigrated project, never silent global.
    observed = _bare("deltalegacycmd", cwd=project, csk_home=csk_home)
    assert observed.returncode == 1, (observed.returncode, observed.stdout)
    assert "before dispatch records existed" in observed.stderr
    assert "'csk install'" in observed.stderr
    assert "LEGACY-PROJECT" not in observed.stdout
    assert "GLOBAL" not in observed.stdout.splitlines()

    # The global fallback still serves outside the project.
    outside = tmp_path / "outside"
    outside.mkdir()
    proc = _bare("deltalegacycmd", cwd=outside, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("GLOBAL")

    # Status reports the migration debt as a problem.
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    assert cli.main(["status", "app", "--check"]) == 1
    captured = capsys.readouterr()
    assert "before dispatch records existed" in captured.err

    # Reinstalling migrates: the record clears and the v1 pin serves.
    results = installer.install(cfg)
    assert not results[0].errors, results[0].errors
    registry, _generation = _read_registry(csk_home)
    entries = list(registry["projects"].values())
    assert len(entries) == 1
    assert entries[0]["needs_migration"] is False
    migrated = _bare("deltalegacycmd", cwd=project, csk_home=csk_home)
    assert migrated.returncode == 0, migrated.stderr
    assert migrated.stdout.startswith("LEGACY-PROJECT")
    assert cli.main(["status", "app", "--check"]) == 0


def test_d7_never_installed_project_gets_plain_empty_scope(
    tmp_path, skills_root, csk_home
):
    """D7 contrast: a never-installed project keeps silent fallback.

    A configured project with no install history gets a plain empty
    scope (not a migration tombstone): calls inside it use the global
    fallback. Production call site: ``dispatch.publish_legacy_scopes``.
    """
    project = make_project(tmp_path, "fresh")
    _make_command_skill(
        skills_root, "skill-g", {"gtool": "GLOBAL-G"}, tag="v1"
    )
    _install_global(
        csk_home, skills_root, project, [{"name": "skill-g", "tag": "v1"}]
    )
    registry, _generation = _read_registry(csk_home)
    entries = list(registry["projects"].values())
    assert len(entries) == 1
    assert entries[0]["needs_migration"] is False
    assert entries[0]["commands"] == {}
    proc = _bare("gtool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("GLOBAL-G")


def test_d7_malformed_consumers_warns_and_falls_back(
    tmp_path, skills_root, csk_home
):
    """D7: a malformed consumers record warns, never silently empties.

    The legacy scan reports the unreadable record and treats the
    project as never installed (plain empty scope, global fallback).
    Production call site: ``dispatch._installed_consumer_roots``.
    """
    project = make_project(tmp_path, "fresh")
    (csk_home / "consumers.json").write_text("{malformed", encoding="utf-8")
    _make_command_skill(
        skills_root, "skill-g", {"gtool": "GLOBAL-G"}, tag="v1"
    )
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    global_root = csk_home / "global"
    global_root.mkdir(parents=True, exist_ok=True)
    (global_root / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agents": ["claude_code"],
                "skills": [{"name": "skill-g", "tag": "v1"}],
            }
        ),
        encoding="utf-8",
    )
    result = global_install.install(cfg)
    assert not result.errors, result.errors
    assert any("consumers record" in message for message in result.messages), (
        result.messages
    )
    registry, _generation = _read_registry(csk_home)
    entries = list(registry["projects"].values())
    assert len(entries) == 1
    assert entries[0]["needs_migration"] is False
    proc = _bare("gtool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("GLOBAL-G")


def _require_hdiutil() -> None:
    if sys.platform != "darwin" or shutil.which("hdiutil") is None:
        pytest.skip("needs macOS hdiutil for case-sensitive images")


def _attach_apfsx_volume(label: str) -> tuple[str, str]:
    """Attach a 256MB case-sensitive APFS ramdisk; return (device, mount).

    Callers must detach the device in a finally block.
    """
    proc = subprocess.run(
        ["hdiutil", "attach", "-nomount", "ram://524288"],
        capture_output=True,
        text=True,
        check=True,
    )
    device = proc.stdout.strip().split()[-1]
    subprocess.run(
        ["diskutil", "eraseDisk", "APFSX", label, device],
        capture_output=True,
        text=True,
        check=True,
    )
    mount = f"/Volumes/{label}"
    assert _dispatch_runtime.volume_case_insensitive(
        mount, manager_home=os.path.expanduser("~")
    ) is False, f"{mount} is not case-sensitive"
    return device, mount


def test_d1_case_sensitive_sibling_uses_global_natively(
    tmp_path, skills_root, csk_home
):
    """D1 native: a case-variant sibling on a sensitive volume falls back.

    Source: delta ``test_reviewer_case_sensitive_sibling_uses_global``
    (the ``filesystem-case-root-identity`` verdict finding). On a
    task-owned case-sensitive APFS image, a directory whose name
    differs from the registered root only by case is a different
    directory: dispatch there uses the global fallback instead of the
    pin (and instead of refusing). Production call site: bare shim ->
    ``_dispatch_runtime.find_project``.
    """
    _require_hdiutil()
    label = f"cskd1-{os.getpid()}"
    device, mount = _attach_apfsx_volume(label)
    try:
        root = init_git_repo(Path(mount) / "DeltaCaseRoot")
        write_files(
            root,
            {
                ".gitignore": ".agents/\n.claude/skills/\n.codex/skills/\n"
                ".gemini/skills/\n.cursor/rules/\n",
            },
        )
        commit_all(root, "gitignore")
        sibling = Path(mount) / "deltacaseroot"
        sibling.mkdir()
        assert not sibling.samefile(root)
        repo, _commit = _make_command_skill(
            skills_root, "skill-d1", {"d1cmd": "PROJECT"}, tag="v1"
        )
        _retitle_skill(repo, {"d1cmd": "GLOBAL"}, tag="v2")
        write_skillfile(
            root,
            {
                "schema_version": 1,
                "agents": ["claude_code"],
                "skills": [{"name": "skill-d1", "tag": "v1"}],
            },
        )
        cfg = make_config(csk_home, skills_root, root, agents=["claude_code"])
        results = installer.install(cfg)
        assert not results[0].errors, results[0].errors
        assert results[0].status == "ok", results[0].messages
        _install_global(
            csk_home, skills_root, root, [{"name": "skill-d1", "tag": "v2"}]
        )
        proc = _bare("d1cmd", cwd=sibling, csk_home=csk_home)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert proc.stdout.startswith("GLOBAL"), proc.stdout
        proc = _bare("d1cmd", cwd=root, csk_home=csk_home)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("PROJECT"), proc.stdout
    finally:
        subprocess.run(
            ["hdiutil", "detach", device, "-force"],
            capture_output=True,
        )


def test_d4_stale_record_never_vetoes_other_dispatch(
    tmp_path, skills_root, csk_home
):
    """D4: a renamed root never vetoes an unrelated dispatch.

    Source: delta ``test_reviewer_stale_record_never_vetoes_other_dispatch``
    (the stale-isolation finding): the stale record is ignored for
    matching, so an unrelated directory reports unavailable instead of
    a moved-root error. The cross-volume inode-coincidence shape is
    covered by ``test_r4_cross_volume_inode_coincidence_ignored``.
    Production call site: bare shim ->
    ``_dispatch_runtime.find_project``.
    """
    project = make_project(tmp_path, "proj")
    _make_command_skill(
        skills_root, "skill-d4", {"d4cmd": "PROJECT"}, tag="v1"
    )
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-d4", "tag": "v1"}]
    )
    project.rename(tmp_path / "renamed")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _bare("d4cmd", cwd=elsewhere, csk_home=csk_home)
    assert proc.returncode == 127, (proc.returncode, proc.stdout, proc.stderr)
    assert "outside every" in proc.stderr


def test_d4_stale_record_never_vetoes_other_volume_natively(
    tmp_path, skills_root, csk_home
):
    """D4 native: a stale record on one volume never vetoes another.

    Same class as ``test_d4_stale_record_never_vetoes_other_dispatch``
    with the unrelated directory on a separate task-owned volume.
    Production call site: bare shim ->
    ``_dispatch_runtime.find_project``.
    """
    _require_hdiutil()
    label = f"cskd4-{os.getpid()}"
    device, mount = _attach_apfsx_volume(label)
    try:
        project = make_project(tmp_path, "proj")
        _make_command_skill(
            skills_root, "skill-d4x", {"d4xcmd": "PROJECT"}, tag="v1"
        )
        _install_project(
            csk_home, skills_root, project, [{"name": "skill-d4x", "tag": "v1"}]
        )
        project.rename(tmp_path / "renamed")
        elsewhere = Path(mount) / "elsewhere"
        elsewhere.mkdir()
        proc = _bare("d4xcmd", cwd=elsewhere, csk_home=csk_home)
        assert proc.returncode == 127, (proc.returncode, proc.stderr)
        assert "outside every" in proc.stderr
    finally:
        subprocess.run(
            ["hdiutil", "detach", device, "-force"],
            capture_output=True,
        )


def test_publish_and_load_refuse_line_breaks_in_root_and_target(tmp_path):
    """Roots and targets with line breaks are refused, never recorded.

    The resolve protocol is line-based, so publication refuses newline
    and carriage-return bytes in canonical roots and activation
    targets; the loader rejects them as corruption too, so a
    hand-edited registry cannot smuggle an ambiguous protocol line.
    Production call sites: ``dispatch._project_entry``,
    ``dispatch._validated_target``,
    ``_dispatch_runtime.validate_registry_data``.
    """
    home = tmp_path / "manager"
    locking.provision_new_manager_home(home)
    root = tmp_path / "proj"
    root.mkdir()
    target = home / "bin-target"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    _checked, digest = dispatch._validated_target(
        home, str(target), name="nlcmd"
    )
    skills = [{"name": "skill-nl", "commit": "abc123"}]
    commands = {"nlcmd": ("skill-nl", str(target), "script")}
    with pytest.raises(dispatch.DispatchPublishError, match="line break"):
        dispatch._project_entry(
            home,
            canonical_root=str(root) + "\n",
            project_alias="app",
            checkout_alias=None,
            skills=skills,
            commands=commands,
            existing_projects={},
        )
    bad_target_commands = {
        "nlcmd": ("skill-nl", str(target) + "\n", "script")
    }
    with pytest.raises(dispatch.DispatchPublishError, match="line break"):
        dispatch.publish_project(
            home,
            canonical_root=str(root),
            project_alias="app",
            checkout_alias=None,
            skills=skills,
            commands=bad_target_commands,
        )
    bad_cr_commands = {
        "nlcmd": ("skill-nl", str(target) + "\r", "script")
    }
    with pytest.raises(dispatch.DispatchPublishError, match="line break"):
        dispatch.publish_project(
            home,
            canonical_root=str(root),
            project_alias="app",
            checkout_alias=None,
            skills=skills,
            commands=bad_cr_commands,
        )
    # Loader defense in depth: forged line breaks are corruption.
    forged = _dispatch_runtime.empty_registry()
    forged["projects"] = {
        "a" * 64: {
            "checkout_id": "a" * 64,
            "canonical_root": str(root),
            "project_alias": "app",
            "checkout_alias": None,
            "root_identity": {"st_ino": str(root.stat().st_ino)},
            "needs_migration": False,
            "skills": skills,
            "commands": {
                "nlcmd": {
                    "owner": "skill-nl",
                    "target": str(target) + "\n",
                    "kind": "script",
                    "digest": digest,
                }
            },
        }
    }
    with pytest.raises(DispatchError) as excinfo:
        _dispatch_runtime.validate_registry_data(forged, generation_id="x")
    assert excinfo.value.kind == "registry_corrupt"
    assert "line break" in str(excinfo.value)


def test_resolve_checkout_id_dedup_follows_volume_rule(tmp_path):
    """Checkout-id dedup reuses the id only under the volume's rule.

    The same directory under a case-variant spelling dedupes when the
    volume folds case and registers distinctly when it does not; exact
    spellings always dedupe and different inodes never do. This direct
    unit test is the volume-independent killer for the dedup-narrowing
    mutant. Production call site:
    ``_dispatch_runtime.resolve_checkout_id``.
    """
    root = tmp_path / "Proj"
    root.mkdir()
    ino = root.stat().st_ino
    projects = {
        "a" * 64: {
            "checkout_id": "a" * 64,
            "canonical_root": str(root),
            "root_identity": {"st_ino": str(ino)},
        }
    }
    swapped = str(root)[:-4] + "pROJ"
    assert swapped != str(root)
    assert (
        _dispatch_runtime.resolve_checkout_id(
            projects,
            canonical_root=str(root),
            root_ino=ino,
            case_insensitive=False,
        )
        == "a" * 64
    )
    assert (
        _dispatch_runtime.resolve_checkout_id(
            projects,
            canonical_root=swapped,
            root_ino=ino,
            case_insensitive=True,
        )
        == "a" * 64
    )
    assert (
        _dispatch_runtime.resolve_checkout_id(
            projects,
            canonical_root=swapped,
            root_ino=ino,
            case_insensitive=False,
        )
        != "a" * 64
    )
    assert (
        _dispatch_runtime.resolve_checkout_id(
            projects,
            canonical_root=str(root),
            root_ino=ino + 1,
            case_insensitive=True,
        )
        != "a" * 64
    )


def test_env_generated_transparency_matches_direct(tmp_path, skills_root, csk_home):
    """Generated R11: random caller environments pass through byte-identical.

    Twenty-five seeded hostile environments (sentinel-like keys,
    locale variables, empty and unicode values, whitespace and line
    breaks) observed through a shell target: direct and dispatched
    runs agree byte-for-byte apart from the registry-derived
    ``CSK_PROJECT_ROOT``. Caller-owned ``_CSK_*`` keys survive
    untouched; no dispatcher-private key appears. This closes the
    ``caller-environment-clobbered`` class. Production call site: bare
    shim -> ``exec "$target"``.
    """
    rng = random.Random(20261004)
    project = make_project(tmp_path, "proj-p")
    files: dict[str, str | bytes] = {
        "csk-skill.json": json.dumps(
            {
                "schema_version": 1,
                "commands": {
                    "envdump": {
                        "type": "script",
                        "unix_path": "scripts/envdump",
                    }
                },
            }
        ),
        "scripts/envdump": "#!/bin/sh\nenv -0\n",
    }
    make_skill_repo(skills_root, "skill-envgen", files, tag="v1")
    _install_project(
        csk_home, skills_root, project, [{"name": "skill-envgen", "tag": "v1"}]
    )
    registry, _generation = _read_registry(csk_home)
    entry = next(iter(registry["projects"].values()))
    target = entry["commands"]["envdump"]["target"]
    shims_path = os.environ["PATH"] + os.pathsep + str(
        dispatch.shims_dir(csk_home)
    )
    hostile_keys = [
        "LC_CTYPE",
        "LC_ALL",
        "LANG",
        "__CF_USER_TEXT_ENCODING",
        "_CSK_PYTHON",
        "_CSK_RUNTIME",
        "_CSK_DISPATCHER",
        "_CSK_COMMAND",
        "_CSK_ENV_VAL_LC_CTYPE",
        "_CSK_ENV_SET_CF",
        "STICKY",
        "EMPTY_OK",
        "SPACE KEY",
        "UNICODE",
    ]
    hostile_values = [
        "caller-value",
        "",
        "C",
        "en_US.UTF-8",
        "snowman-\u2603",
        "with space",
        "quote'\"dollar$back\\slash",
        "new\nline",
        "tab\there",
        "trailing-space ",
        "semicolon;pipe|amp&",
    ]
    seen_sentinel_like = False
    for trial in range(25):
        base = {"PATH": shims_path}
        for key in rng.sample(hostile_keys, k=rng.randint(3, 8)):
            base[key] = rng.choice(hostile_values)
            if key.startswith("_CSK_"):
                seen_sentinel_like = True
        direct = subprocess.run(
            [target], cwd=project, env=dict(base), capture_output=True
        )
        via = subprocess.run(
            ["sh", "-c", 'exec "$@"', "sh", "envdump"],
            cwd=project,
            env=dict(base),
            capture_output=True,
        )
        assert direct.returncode == 0, (trial, direct.stderr)
        assert via.returncode == 0, (trial, via.stderr)

        def parse(blob: bytes) -> dict[bytes, bytes]:
            parsed: dict[bytes, bytes] = {}
            for item in blob.split(b"\0"):
                if not item or b"=" not in item:
                    continue
                key, _, value = item.partition(b"=")
                parsed[key] = value
            return parsed

        direct_env = parse(direct.stdout)
        via_env = parse(via.stdout)
        assert via_env.pop(b"CSK_PROJECT_ROOT").decode() == (
            entry["canonical_root"]
        ), trial
        assert b"CSK_PROJECT_ROOT" not in direct_env, trial
        for worker in (direct_env, via_env):
            worker.pop(b"_", None)
            worker.pop(b"PATH", None)
        assert via_env == direct_env, trial
    assert seen_sentinel_like
