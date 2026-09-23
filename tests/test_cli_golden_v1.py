"""Byte-identical released v1 CLI behaviour without the opt-in.

The committed files in ``tests/fixtures/cli-golden-v1/`` preserve the original
capture from released commit ``23a70734a38a65ad1c236a4315a0e2603b1f90b6``.
The active gate renders that released source and the candidate under the same
Python interpreter because argparse help wrapping changed after Python 3.12.
Both runs use the terminal width from ``base_run_env`` and capture through
``StringIO(newline="\\n")``; line endings remain byte-exact and only path
separators are normalized.

The command matrix covers every v1 command, including all ``--help`` texts.
The draft ``csk check`` subcommand does not exist in the release, and its
absence remains covered by ``test_cli_check_absent_without_opt_in`` in
``tests/test_cli.py``.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

from cli_golden_v1_support import (
    COMMITTED_COMMANDS,
    CLIOutput,
    PINNED_COLUMNS,
    base_run_env,
    build_pristine_fixture,
    extract_release_source,
    render_argv,
    repoint_config_paths,
    repoint_run_copy,
    run_cli,
    tokenize_output,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = Path(__file__).parent / "fixtures" / "cli-golden-v1"
EXPECTED_GOLDEN_COMMIT = "23a70734a38a65ad1c236a4315a0e2603b1f90b6"


def _normalize(data: bytes) -> bytes:
    """Normalize path separators only; line endings are deliberately exact."""
    if os.sep != "/":
        return data.replace(os.sep.encode("utf-8"), b"/")
    return data


@pytest.fixture(scope="module")
def pristine_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = Path(os.path.realpath(tmp_path_factory.mktemp("golden-v1-pristine")))
    build_pristine_fixture(root)
    return root


@pytest.fixture(scope="module")
def released_v1_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    destination = tmp_path_factory.mktemp("released-v1-source")
    return extract_release_source(
        EXPECTED_GOLDEN_COMMIT, destination, cwd=PROJECT_ROOT
    )


def _command_ids() -> list[str]:
    return [command.slug for command in COMMITTED_COMMANDS]


def _run_command_copy(
    source_root: Path,
    command_slug: str,
    *,
    pristine_root: Path,
    tmp_path: Path,
    label: str,
) -> tuple[CLIOutput, Path]:
    command = next(item for item in COMMITTED_COMMANDS if item.slug == command_slug)
    run_root = Path(os.path.realpath(tmp_path / f"{label}-{command_slug}"))
    shutil.copytree(pristine_root, run_root, symlinks=True)
    repoint_run_copy(run_root, pristine_root)

    env = base_run_env(run_root)
    for key, value in command.env:
        env[key] = (
            value.replace("{ROOT}", str(run_root)) if value is not None else None
        )
    cwd = run_root / command.cwd_rel if command.cwd_rel else run_root
    return run_cli(source_root, render_argv(command, run_root), cwd=cwd, env=env), run_root


@pytest.mark.parametrize("slug", _command_ids())
def test_cli_v1_golden_byte_identical(
    slug: str,
    pristine_root: Path,
    released_v1_source: Path,
    tmp_path: Path,
) -> None:
    candidate, candidate_root = _run_command_copy(
        PROJECT_ROOT / "src",
        slug,
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="candidate",
    )
    released, released_root = _run_command_copy(
        released_v1_source,
        slug,
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="released",
    )

    assert candidate.exit_code == released.exit_code
    assert _normalize(
        tokenize_output(
            candidate.stdout, root=str(candidate_root), version=candidate.version
        )
    ) == _normalize(
        tokenize_output(released.stdout, root=str(released_root), version=released.version)
    )
    assert _normalize(
        tokenize_output(
            candidate.stderr, root=str(candidate_root), version=candidate.version
        )
    ) == _normalize(
        tokenize_output(released.stderr, root=str(released_root), version=released.version)
    )


def test_golden_fixture_inventory_is_complete() -> None:
    slugs = sorted(command.slug for command in COMMITTED_COMMANDS)
    assert slugs, "the golden command matrix must not be empty"
    for slug in slugs:
        for suffix in (".stdout", ".stderr", ".exit"):
            assert (GOLDEN_DIR / f"{slug}{suffix}").exists(), slug
    assert EXPECTED_GOLDEN_COMMIT == "23a70734a38a65ad1c236a4315a0e2603b1f90b6"


def test_base_run_env_pins_terminal_width(tmp_path: Path) -> None:
    assert base_run_env(tmp_path)["COLUMNS"] == PINNED_COLUMNS


def test_cli_run_observes_pinned_terminal_width(
    pristine_root: Path, tmp_path: Path
) -> None:
    result, _ = _run_command_copy(
        PROJECT_ROOT / "src",
        "bare",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="pinned-width",
    )
    assert result.terminal_width == int(PINNED_COLUMNS)


def test_cli_capture_keeps_lf_bytes_on_every_platform(
    pristine_root: Path, tmp_path: Path
) -> None:
    result, _ = _run_command_copy(
        PROJECT_ROOT / "src",
        "bare",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="line-ending",
    )
    assert result.exit_code == 0
    assert b"\n" in result.stdout
    assert b"\r" not in result.stdout


def test_fixture_text_inputs_keep_lf_bytes(pristine_root: Path) -> None:
    fixture_files = (
        pristine_root / "home" / "config.json",
        pristine_root / "project" / ".gitignore",
        pristine_root / "project" / "Skillfile.json",
        pristine_root / "skilldir" / "SKILL.md",
        pristine_root / "skills" / "demo-skill" / "SKILL.md",
    )
    for path in fixture_files:
        assert b"\r" not in path.read_bytes(), path


def test_repoint_run_copy_rewrites_paths_to_the_run_directory(tmp_path: Path) -> None:
    pristine_root = Path(os.path.realpath(tmp_path / "pristine"))
    build_pristine_fixture(pristine_root)
    run_root = Path(os.path.realpath(tmp_path / "run"))
    shutil.copytree(pristine_root, run_root, symlinks=True)

    repoint_run_copy(run_root, pristine_root)

    config = json.loads((run_root / "home" / "config.json").read_text(encoding="utf-8"))
    assert config["skills_root"] == (run_root / "skills").as_posix()
    assert config["projects"]["demo"]["path"] == (run_root / "project").as_posix()


def test_repoint_config_paths_handles_windows_json_paths() -> None:
    config: dict[str, Any] = {
        "skills_root": "D:/a/pristine/skills",
        "projects": {"demo": {"path": "D:/a/pristine/project"}},
    }
    repoint_config_paths(
        config,
        PureWindowsPath("D:/a/pristine"),
        PureWindowsPath("D:/b/run"),
    )
    assert config["skills_root"] == "D:/b/run/skills"
    assert config["projects"]["demo"]["path"] == "D:/b/run/project"


def test_audit_alias_does_not_observe_add_skill_from_another_case(
    pristine_root: Path, tmp_path: Path
) -> None:
    added, _ = _run_command_copy(
        PROJECT_ROOT / "src",
        "add-skill",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="add-first",
    )
    assert added.exit_code == 0
    assert "extra-skill" not in (pristine_root / "project" / "Skillfile.json").read_text(
        encoding="utf-8"
    )

    audited, _ = _run_command_copy(
        PROJECT_ROOT / "src",
        "audit-alias",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="audit-next",
    )
    assert audited.stdout == (
        b"demo: demo-skill tag v1.0.0 b94019b allow (0 finding(s))\n"
    )
    assert b"extra-skill" not in audited.stdout
