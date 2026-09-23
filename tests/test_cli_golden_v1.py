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
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

import cli_golden_v1_support as golden_support
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


def _normalize(data: bytes, *, separator: str = os.sep) -> bytes:
    """Normalize text paths only; preserve JSON escaping and exact line endings."""
    if separator != "/":
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        else:
            # JSON backslash pairs encode path separators; replacing them here
            # would turn ``\\\\`` into ``//`` and corrupt the compared value.
            return data
        return data.replace(separator.encode("utf-8"), b"/")
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


@pytest.mark.parametrize(
    "ambient_columns",
    [None, "80", "200"],
    ids=["unset", "same-as-pin", "wide-terminal"],
)
def test_golden_cli_uses_pinned_width_despite_ambient_columns(
    ambient_columns: str | None,
    pristine_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if ambient_columns is None:
        monkeypatch.delenv("COLUMNS", raising=False)
    else:
        monkeypatch.setenv("COLUMNS", ambient_columns)

    result, _ = _run_command_copy(
        PROJECT_ROOT / "src",
        "bare",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="ambient-width",
    )
    assert result.terminal_width == int(PINNED_COLUMNS)


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


def test_cli_capture_pins_lf_with_a_windows_default_text_stream(
    pristine_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture stream explicitly uses LF even if ambient text defaults to CRLF."""
    sitecustomize_dir = tmp_path / "windows-text-default"
    sitecustomize_dir.mkdir()
    (sitecustomize_dir / "sitecustomize.py").write_text(
        "import io\n"
        "_StringIO = io.StringIO\n"
        "class WindowsDefaultStringIO(_StringIO):\n"
        "    def __init__(self, initial_value='', newline='\\r\\n'):\n"
        "        super().__init__(initial_value, newline=newline)\n"
        "io.StringIO = WindowsDefaultStringIO\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setenv("PYTHONPATH", str(sitecustomize_dir))

    result, _ = _run_command_copy(
        PROJECT_ROOT / "src",
        "bare",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="windows-newline-default",
    )

    assert result.exit_code == 0
    assert b"\n" in result.stdout
    assert b"\r" not in result.stdout
    assert b"\r" not in result.stderr


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


def test_status_json_tokenizes_windows_roots_without_rewriting_escapes() -> None:
    root = r"D:\a\candidate"
    payload = json.dumps(
        {
            "native": root + r"\project\Skillfile.json",
            "posix": root.replace("\\", "/") + "/project/Skillfile.json",
        },
        sort_keys=True,
    ).encode("utf-8")

    tokenized = tokenize_output(payload, root=root, version="1.0.0")
    normalized = _normalize(tokenized, separator="\\")

    assert json.loads(normalized) == {
        "native": "{{GOLDEN_ROOT}}\\project\\Skillfile.json",
        "posix": "{{GOLDEN_ROOT}}/project/Skillfile.json",
    }
    assert b"//project" not in normalized
    assert _normalize(b"Wrote D:\\a\\candidate", separator="\\") == (
        b"Wrote D:/a/candidate"
    )


def test_status_json_golden_rejects_one_byte_product_drift_at_alternate_width(
    pristine_root: Path,
    released_v1_source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real alias-byte change fails the named gate on Python 3.14/width 120."""
    mutant_project_root = tmp_path / "mutant-project"
    mutant_source = mutant_project_root / "src"
    shutil.copytree(PROJECT_ROOT / "src", mutant_source)
    status_source = mutant_source / "csk" / "status.py"
    original = status_source.read_bytes()
    needle = b'"alias": project.alias,'
    replacement = b'"alias": project.alias + "x",'
    assert original.count(needle) == 1
    status_source.write_bytes(original.replace(needle, replacement, 1))

    # Exercise the same shared environment seam at a width other than its
    # normal 80-column setting. Candidate and released children both receive
    # this width; the one-byte behavior change remains visible to the gate.
    monkeypatch.setattr(golden_support, "PINNED_COLUMNS", "120")
    monkeypatch.setattr(sys.modules[__name__], "PROJECT_ROOT", mutant_project_root)

    candidate, candidate_root = _run_command_copy(
        mutant_source,
        "status-json",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="one-byte-mutant",
    )
    released, released_root = _run_command_copy(
        released_v1_source,
        "status-json",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="one-byte-reference",
    )
    assert candidate.terminal_width == 120
    assert released.terminal_width == 120
    candidate_bytes = _normalize(
        tokenize_output(
            candidate.stdout, root=str(candidate_root), version=candidate.version
        )
    )
    released_bytes = _normalize(
        tokenize_output(
            released.stdout, root=str(released_root), version=released.version
        )
    )
    assert len(candidate_bytes) == len(released_bytes) + 1
    assert candidate_bytes == released_bytes.replace(
        b'"alias": "demo"', b'"alias": "demox"', 1
    )

    with pytest.raises(AssertionError):
        test_cli_v1_golden_byte_identical(
            "status-json",
            pristine_root,
            released_v1_source,
            tmp_path,
        )


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

    audited, audit_root = _run_command_copy(
        PROJECT_ROOT / "src",
        "audit-alias",
        pristine_root=pristine_root,
        tmp_path=tmp_path,
        label="audit-next",
    )
    expected_content = b"demo: demo-skill tag v1.0.0 b94019b allow (0 finding(s))"
    assert tokenize_output(
        audited.stdout.removesuffix(b"\n").removesuffix(b"\r"),
        root=str(audit_root),
        version=audited.version,
    ) == expected_content
    assert audited.stdout == expected_content + b"\n"
    assert audited.stdout.endswith(b"\n")
    assert b"\r" not in audited.stdout
    assert audited.stdout[:-1] == (
        b"demo: demo-skill tag v1.0.0 b94019b allow (0 finding(s))"
    )
    assert b"extra-skill" not in audited.stdout
