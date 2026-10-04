"""Project-aware POSIX dispatch: generations, shims, dispatcher.

Slice TASK-261004-14uqq0, design part A. Every behavior here is driven
through a real ``csk install`` / ``csk global install`` plus real subprocess
bare calls; helper-level checks supplement but never replace that path.
"""

from __future__ import annotations

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

from csk import _dispatch_runtime, dispatch, global_install, installer
from csk._dispatch_runtime import DispatchError

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
    "damage", ["missing", "not-regular", "not-executable", "symlink-escape"]
)
def test_corrupt_pinned_target_refuses_without_global_fallback(
    tmp_path, skills_root, csk_home, damage
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-p", {"ctool": "PROJECT-C"}, tag="v1")
    _make_command_skill(skills_root, "skill-g", {"ctool": "GLOBAL-C"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-g", "tag": "v1"}])
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

    proc = _bare("ctool", cwd=project, csk_home=csk_home)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "GLOBAL-C" not in proc.stdout
    assert "PROJECT-C" not in proc.stdout
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
    _make_command_skill(skills_root, "skill-p", {"etool": "PREFIX-P"}, tag="v1")
    _make_command_skill(skills_root, "skill-g", {"etool": "PREFIX-G"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-g", "tag": "v1"}])

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
    _make_command_skill(skills_root, "skill-p", {"ntool": "PARENT-N"}, tag="v1")
    _make_command_skill(skills_root, "skill-g", {"ntool": "GLOBAL-N"}, tag="v1")
    _install_project(csk_home, skills_root, parent, [{"name": "skill-p", "tag": "v1"}])
    _install_global(csk_home, skills_root, parent, [{"name": "skill-g", "tag": "v1"}])

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


def test_project_wins_over_global_for_different_skills_sharing_a_name(
    tmp_path, skills_root, csk_home
):
    project = make_project(tmp_path, "proj-p")
    _make_command_skill(skills_root, "skill-pa", {"shared": "PROJECT-SHARED"}, tag="v1")
    _make_command_skill(skills_root, "skill-ga", {"shared": "GLOBAL-SHARED"}, tag="v1")
    _install_project(csk_home, skills_root, project, [{"name": "skill-pa", "tag": "v1"}])
    _install_global(csk_home, skills_root, project, [{"name": "skill-ga", "tag": "v1"}])

    proc = _bare("shared", cwd=project, csk_home=csk_home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "PROJECT-SHARED"

    outside = tmp_path / "outside"
    outside.mkdir()
    proc_outside = _bare("shared", cwd=outside, csk_home=csk_home)
    assert proc_outside.returncode == 0, proc_outside.stderr
    assert proc_outside.stdout.splitlines()[0] == "GLOBAL-SHARED"


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


def test_find_project_matches_by_component_not_string_prefix():
    projects = {
        "a" * 64: {"checkout_id": "a" * 64, "canonical_root": "/work/proj"},
        "b" * 64: {"checkout_id": "b" * 64, "canonical_root": "/work/proj-evil"},
    }
    find = _dispatch_runtime.find_project
    assert find(projects, "/work/proj-evil/deep")["canonical_root"] == "/work/proj-evil"
    assert find(projects, "/work/proj/sub")["canonical_root"] == "/work/proj"
    assert find(projects, "/work/proj")["canonical_root"] == "/work/proj"
    assert find(projects, "/work/proj/./sub")["canonical_root"] == "/work/proj"
    assert find(projects, "/work/other") is None
    assert find(projects, "/work/proj-evil")["canonical_root"] == "/work/proj-evil"


def test_contradictory_registrations_for_one_root_refuse():
    projects = {
        "a" * 64: {"checkout_id": "a" * 64, "canonical_root": "/work/x"},
        "b" * 64: {"checkout_id": "b" * 64, "canonical_root": "/work/x"},
    }
    with pytest.raises(DispatchError, match="contradictory"):
        _dispatch_runtime.find_project(projects, "/work/x/sub")


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

    def random_decls() -> list[dict[str, str]] | None:
        roll = rng.random()
        if roll < 0.15:
            return []
        if roll < 0.30:
            return None
        pool: list[dict[str, str]] = []
        if rng.random() < 0.5:
            pool.append({"name": "skill-alpha", "tag": "v1"})
        else:
            pool.append(
                {"name": "skill-beta", "tag": rng.choice(beta_tags)}
            )
        if rng.random() < 0.5:
            pool.append({"name": "skill-gamma", "tag": "v1"})
        return pool

    for trial in range(4):
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
            else:
                assert proc.returncode == 127, context
                # The dispatcher reports "unavailable" when a shim exists
                # but the effective scope lacks the name; the shell
                # reports "not found" when no layer exports it at all.
                assert (
                    "unavailable" in proc.stderr or "not found" in proc.stderr
                ), context


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
