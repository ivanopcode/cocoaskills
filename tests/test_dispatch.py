"""Project-aware POSIX dispatch: generations, shims, dispatcher.

Slice TASK-261004-14uqq0, design part A. Every behavior here is driven
through a real ``csk install`` / ``csk global install`` plus real subprocess
bare calls; helper-level checks supplement but never replace that path.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
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


def test_install_publishes_dispatch_generation_and_posix_shims(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-tool", {"dtool": "DTOOL-V1"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-tool", "tag": "v1"}])

    registry, generation = _read_registry(csk_home)
    assert registry["schema_version"] == 1
    assert len(generation) == 32
    assert sorted(registry) == ["global", "projects", "schema_version"]
    assert len(registry["projects"]) == 1
    entry = next(iter(registry["projects"].values()))
    canonical = str(project.resolve())
    assert entry["canonical_root"] == canonical
    assert entry["checkout_id"] == dispatch.checkout_id_for_root(canonical)
    assert len(entry["checkout_id"]) == 64
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
    assert (digest["st_dev"], digest["st_ino"]) == (
        str(info.st_dev),
        str(info.st_ino),
    )
    assert (digest["size"], digest["mtime_ns"]) == (
        str(info.st_size),
        str(info.st_mtime_ns),
    )
    root_info = Path(canonical).stat()
    assert entry["root_identity"] == {
        "st_dev": str(root_info.st_dev),
        "st_ino": str(root_info.st_ino),
    }

    shim = dispatch.shims_dir(csk_home) / "dtool"
    assert shim.is_file()
    assert os.access(shim, os.X_OK)
    dispatcher = dispatch.dispatcher_path(csk_home)
    assert dispatcher.is_file()
    assert os.access(dispatcher, os.X_OK)

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
    nested_entry = registry["projects"][
        dispatch.checkout_id_for_root(str(nested.resolve()))
    ]
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
            "st_dev": str(info.st_dev),
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


def test_contradictory_registrations_for_one_root_refuse():
    projects = {
        "a" * 64: {
            "checkout_id": "a" * 64,
            "canonical_root": "/work/x",
            "root_identity": {"st_dev": "1", "st_ino": "1"},
        },
        "b" * 64: {
            "checkout_id": "b" * 64,
            "canonical_root": "/work/x",
            "root_identity": {"st_dev": "1", "st_ino": "2"},
        },
    }
    with pytest.raises(DispatchError, match="contradictory"):
        _dispatch_runtime.find_project(projects, "/work/x/sub")


def test_contradictory_registrations_for_one_directory_refuse():
    projects = {
        "a" * 64: {
            "checkout_id": "a" * 64,
            "canonical_root": "/work/a",
            "root_identity": {"st_dev": "7", "st_ino": "7"},
        },
        "b" * 64: {
            "checkout_id": "b" * 64,
            "canonical_root": "/work/b",
            "root_identity": {"st_dev": "7", "st_ino": "7"},
        },
    }
    with pytest.raises(DispatchError, match="contradictory"):
        _dispatch_runtime.find_project(projects, "/work/a/sub")


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

    dispatcher_bytes = dispatch.dispatcher_path(csk_home).read_bytes()
    first_line, _, rest = dispatcher_bytes.partition(b"\n")
    assert first_line == b"#!" + os.fsencode(sys.executable) + b" -I"
    assert sys.executable.startswith("/")
    assert b"from csk" not in rest
    assert b"import csk" not in rest


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
    nested_entry = registry["projects"][
        dispatch.checkout_id_for_root(str(nested.resolve()))
    ]
    assert nested_entry["skills"] == []
    assert nested_entry["commands"] == {}

    # Reinstalling only the parent keeps the registered-never-installed
    # nested scope as a boundary: global fallback, never the parent set.
    results = installer.install(load_config(), alias="app")
    assert not results[0].errors, results[0].errors
    registry, _generation = _read_registry(csk_home)
    assert (
        dispatch.checkout_id_for_root(str(nested.resolve())) in registry["projects"]
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
    if (str(live.st_dev), str(live.st_ino)) == (
        recorded["st_dev"],
        recorded["st_ino"],
    ):
        # Stated bound: the filesystem recycled the directory identity,
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
    assert (info.st_dev, info.st_ino) == (
        int(recorded["st_dev"]),
        int(recorded["st_ino"]),
    )
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
    checkout_id = dispatch.checkout_id_for_root(str(ancestor))
    registry["projects"][checkout_id] = {
        "checkout_id": checkout_id,
        "canonical_root": str(ancestor),
        "project_alias": "ancestor",
        "checkout_alias": None,
        "root_identity": {
            "st_dev": str(info.st_dev),
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

    opened: list[str] = []
    state = {"armed": False}

    def hook(event: str, args: tuple) -> None:
        if state["armed"] and event == "open":
            path = args[0] if args else None
            if isinstance(path, str):
                opened.append(path)

    sys.addaudithook(hook)
    calls: list[tuple] = []

    def fake_execve(path: str, argv: list[str], env: dict) -> None:
        calls.append((path, argv, env))
        raise _ExecveIntercepted

    monkeypatch.setattr(os, "execve", fake_execve)
    dispatcher = str(dispatch.dispatcher_path(csk_home))

    def run_main(argv: list[str], cwd: Path) -> None:
        here = os.getcwd()
        os.chdir(cwd)
        state["armed"] = True
        try:
            with pytest.raises(_ExecveIntercepted):
                _dispatch_runtime.main(argv)
        finally:
            state["armed"] = False
            os.chdir(here)

    run_main([dispatcher, "htool", "a b"], project)
    assert len(calls) == 1
    target, argv, env = calls.pop()
    assert target == recorded_target
    assert argv == [recorded_target, "a b"]
    assert env["CSK_PROJECT_ROOT"] == canonical

    run_main([dispatcher, "gtool"], hostile)
    assert len(calls) == 1
    target, argv, env = calls.pop()
    assert target == global_target
    assert argv == [global_target]
    assert "CSK_PROJECT_ROOT" not in env

    violations = [
        path
        for path in opened
        if _is_within(path, project) or _is_within(path, hostile)
    ]
    assert violations == []
    assert not (project / "SHOULD-NOT-RUN").exists()


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
