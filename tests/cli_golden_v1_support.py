"""Released v1 CLI comparison support.

Stdlib only: this module never imports ``csk``. Candidate and released source
trees run in separate child processes under the same Python interpreter. The
committed files in ``tests/fixtures/cli-golden-v1`` preserve the original
release capture, while the active comparison renders the pinned release at
runtime so interpreter-specific argparse formatting is compared like-for-like.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePath
from typing import Any

GOLDEN_ROOT_TOKEN = "{{GOLDEN_ROOT}}"
VERSION_TOKEN = "{{VERSION}}"
PATH_HASH_TOKEN = "{{PATH_HASH}}"
PINNED_COLUMNS = "80"

# Fixed author/committer identity for every fixture git object, so commit
# hashes (and the short hashes csk prints) are stable across runs.
PINNED_GIT_DATE = "2019-04-01T00:00:00+00:00"
PINNED_GIT_ENV = {
    "GIT_AUTHOR_DATE": PINNED_GIT_DATE,
    "GIT_COMMITTER_DATE": PINNED_GIT_DATE,
}

SKILL_MD = "---\nname: skill\n---\n\n# Test skill\n"
GITIGNORE_TEXT = ".agents/\n.claude/skills/\n.codex/skills/\n.gemini/skills/\n.cursor/rules/\n"


@dataclass(frozen=True)
class GoldenCommand:
    slug: str
    argv: tuple[str, ...]
    cwd_rel: str | None = None
    env: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class CLIOutput:
    exit_code: int
    stdout: bytes
    stderr: bytes
    version: str
    terminal_width: int


_CAPTURE_CLI_SCRIPT = r"""
import base64
import contextlib
import io
import json
import shutil
import sys

from csk import __version__, cli

protocol_stdout = sys.stdout.buffer
terminal_width = shutil.get_terminal_size().columns
stdout = io.StringIO(newline="\n")
stderr = io.StringIO(newline="\n")
with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
    exit_code = cli.main(sys.argv[1:])
payload = {
    "exit_code": exit_code,
    "stdout": base64.b64encode(stdout.getvalue().encode("utf-8")).decode("ascii"),
    "stderr": base64.b64encode(stderr.getvalue().encode("utf-8")).decode("ascii"),
    "version": __version__,
    "terminal_width": terminal_width,
}
protocol_stdout.write(json.dumps(payload).encode("ascii"))
"""


def _rel(path: str) -> str:
    return "{ROOT}/" + path


# Every csk command exercised against the schema-1 fixture. Commands that
# mutate (install, add, init, ...) each run on a fresh copy of the pristine
# fixture, so every capture is independent.
GOLDEN_COMMANDS: tuple[GoldenCommand, ...] = (
    GoldenCommand("version", ("--version",)),
    GoldenCommand("bare", (), cwd_rel="project"),
    GoldenCommand("help", ("--help",)),
    # Scoped help texts stay byte-identical without the opt-in: the
    # schema-2 workflow paragraphs render only in draft mode, so these
    # four are committed goldens like every other command.
    GoldenCommand("install-help", ("install", "--help")),
    GoldenCommand("upgrade-help", ("upgrade", "--help")),
    GoldenCommand("status-help", ("status", "--help")),
    GoldenCommand("skill-check-help", ("skill", "check", "--help")),
    GoldenCommand("update-help", ("update", "--help")),
    GoldenCommand("list-help", ("list", "--help")),
    GoldenCommand("project-help", ("project", "--help")),
    GoldenCommand("config-help", ("config", "--help")),
    GoldenCommand("shell-init-help", ("shell-init", "--help")),
    GoldenCommand("global-help", ("global", "--help")),
    GoldenCommand("audit-help", ("audit", "--help")),
    GoldenCommand("gc-help", ("gc", "--help")),
    GoldenCommand("add-help", ("add", "--help")),
    GoldenCommand("remove-help", ("remove", "--help")),
    GoldenCommand("hybrid-help", ("hybrid", "--help")),
    GoldenCommand("skill-help", ("skill", "--help")),
    GoldenCommand("bootstrap-help", ("bootstrap", "--help")),
    GoldenCommand("init-help", ("init", "--help")),
    GoldenCommand("install-alias", ("install", "demo")),
    GoldenCommand("install-bare", ("install",), cwd_rel="project"),
    GoldenCommand("install-dry-run", ("install", "demo", "--dry-run")),
    GoldenCommand("upgrade-alias", ("upgrade", "demo")),
    GoldenCommand("update", ("update",)),
    GoldenCommand("status-alias", ("status", "demo")),
    GoldenCommand("status-bare", ("status",), cwd_rel="project"),
    GoldenCommand("status-check", ("status", "demo", "--check")),
    GoldenCommand("status-json", ("status", "demo", "--json")),
    GoldenCommand("status-all", ("status", "--all")),
    GoldenCommand("list", ("list",)),
    GoldenCommand("list-paths", ("list", "--paths")),
    GoldenCommand("project-resolve-alias", ("project", "resolve", "demo")),
    GoldenCommand("project-resolve-dot", ("project", "resolve", "."), cwd_rel="project"),
    GoldenCommand("project-add", ("project", "add", "extra", _rel("project"))),
    GoldenCommand("config-show", ("config", "show")),
    GoldenCommand("config-build-ssh-list", ("config", "build-ssh", "list")),
    GoldenCommand("config-build-https-list", ("config", "build-https", "list")),
    GoldenCommand("gc", ("gc",)),
    GoldenCommand("skill-check-ok", ("skill", "check", _rel("skilldir"))),
    GoldenCommand("skill-check-ok-json", ("skill", "check", _rel("skilldir"), "--json")),
    GoldenCommand("skill-check-project-err", ("skill", "check", _rel("project"))),
    GoldenCommand("init-empty", ("init", _rel("emptydir"))),
    GoldenCommand(
        "add-skill",
        ("add", "extra-skill", "--source", "demo-skill", "--tag", "v1.0.0", "--project", "demo"),
    ),
    GoldenCommand("remove-skill", ("remove", "demo-skill", "--project", "demo")),
    GoldenCommand("global-list", ("global", "list")),
    GoldenCommand("global-init", ("global", "init")),
    GoldenCommand("global-status", ("global", "status")),
    GoldenCommand("hybrid-list", ("hybrid", "list")),
    GoldenCommand("audit-alias", ("audit", "demo")),
    GoldenCommand("shell-init-zsh", ("shell-init", "zsh")),
    GoldenCommand("shell-init-powershell", ("shell-init", "powershell")),
    GoldenCommand(
        "bootstrap-keep",
        ("bootstrap", "--if-missing", "--non-interactive", "--skills-root", _rel("skills")),
    ),
    GoldenCommand(
        "bootstrap-create",
        ("bootstrap", "--non-interactive", "--skills-root", _rel("skills")),
        env=(("CSK_CONFIG", _rel("home-fresh/config.json")),),
    ),
)

#: Every command in the matrix is a committed golden: without the opt-in
#: the candidate renders all of them byte-identically to origin/main.
COMMITTED_COMMANDS: tuple[GoldenCommand, ...] = GOLDEN_COMMANDS


def run_git(args: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> str:
    merged = dict(os.environ)
    merged.update(PINNED_GIT_ENV)
    if env:
        merged.update(env)
    proc = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, env=merged
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {args} failed in {cwd}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], path)
    run_git(["branch", "-M", "main"], path)
    run_git(["config", "user.name", "Golden Test"], path)
    run_git(["config", "user.email", "golden@example.com"], path)
    run_git(["config", "commit.gpgsign", "false"], path)
    run_git(["config", "core.autocrlf", "false"], path)
    run_git(["config", "core.eol", "lf"], path)


def build_pristine_fixture(root: Path) -> dict[str, Path]:
    """Create the deterministic schema-1 fixture under ``root``.

    Returns the interesting paths. ``root`` must not exist yet.
    """
    home = root / "home"
    skills_root = root / "skills"
    project = root / "project"
    skilldir = root / "skilldir"
    emptydir = root / "emptydir"
    pseudo_home = root / "pseudo-home"
    for path in (home, skills_root, skilldir, emptydir, pseudo_home):
        path.mkdir(parents=True, exist_ok=True)

    skill_repo = skills_root / "demo-skill"
    _init_repo(skill_repo)
    (skill_repo / "SKILL.md").write_text(
        SKILL_MD, encoding="utf-8", newline="\n"
    )
    run_git(["add", "."], skill_repo)
    run_git(["commit", "-m", "skill"], skill_repo)
    run_git(["tag", "v1.0.0"], skill_repo)

    _init_repo(project)
    (project / ".gitignore").write_text(
        GITIGNORE_TEXT, encoding="utf-8", newline="\n"
    )
    (project / "Skillfile.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project": {"alias": "demo"},
                "skills": [{"name": "demo-skill", "tag": "v1.0.0"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    run_git(["add", "."], project)
    run_git(["commit", "-m", "project"], project)

    (skilldir / "SKILL.md").write_text(
        SKILL_MD, encoding="utf-8", newline="\n"
    )

    (home / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": skills_root.as_posix(),
                "default_agents": [],
                "projects": {"demo": {"path": project.as_posix()}},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "home": home,
        "skills_root": skills_root,
        "project": project,
        "skilldir": skilldir,
        "emptydir": emptydir,
        "pseudo_home": pseudo_home,
    }


def repoint_run_copy(run_root: Path, old_root: Path) -> None:
    """Rewrite absolute paths in a fixture copy's config to the copy's root.

    ``shutil.copytree`` carries ``config.json`` verbatim, so without this
    every run would operate on the pristine tree and mutations would leak
    across commands. Only the config carries absolute paths.
    """
    config_path = run_root / "home" / "config.json"
    if not config_path.exists():
        raise AssertionError(f"fixture copy has no config to repoint: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    repoint_config_paths(config, old_root, run_root)
    config_path.write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def repoint_config_paths(
    config: dict[str, Any], old_root: PurePath, run_root: PurePath
) -> None:
    """Repoint the fixture's two known absolute paths and assert both rewrites.

    Fixture config paths are serialized with forward slashes on every host.
    Comparing parsed JSON values avoids Windows JSON escaping turning a native
    ``Path`` string into a silent no-op replacement.
    """
    projects = config.get("projects")
    demo = projects.get("demo") if isinstance(projects, dict) else None
    if not isinstance(demo, dict):
        raise AssertionError("fixture config is missing projects.demo")

    old_values = (
        config.get("skills_root"),
        demo.get("path"),
    )
    expected_old_values = (
        (old_root / "skills").as_posix(),
        (old_root / "project").as_posix(),
    )
    if old_values != expected_old_values:
        raise AssertionError(
            "fixture config paths did not match the pristine root: "
            f"expected {expected_old_values!r}, found {old_values!r}"
        )

    new_values = (
        (run_root / "skills").as_posix(),
        (run_root / "project").as_posix(),
    )
    config["skills_root"] = new_values[0]
    demo["path"] = new_values[1]
    if (config["skills_root"], demo["path"]) != new_values:
        raise AssertionError("fixture config paths were not repointed to the run copy")


def base_run_env(root: Path) -> dict[str, str | None]:
    """Pin the golden CLI environment, including argparse's terminal width.

    Both capture and replay use this mapping. CLI text is captured separately
    through ``StringIO(newline="\\n")`` so Windows text-stream translation
    cannot change the compared bytes.
    """
    pseudo_home = str(root / "pseudo-home")
    return {
        "CSK_CONFIG": str(root / "home" / "config.json"),
        "CSK_EXPERIMENTAL_SKILLFILE_SOURCES": None,
        "CSK_SOURCE_POLICY": None,
        "HOME": pseudo_home,
        "USERPROFILE": pseudo_home,
        "LC_ALL": "C",
        "TZ": "UTC",
        "COLUMNS": PINNED_COLUMNS,
        "PYTHONHASHSEED": "0",
    }


def extract_release_source(commit: str, destination: Path, *, cwd: Path) -> Path:
    """Extract one released ``src/csk`` tree without changing the checkout."""
    proc = subprocess.run(
        ["git", "archive", "--format=tar", commit, "src/csk"],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git archive {commit} src/csk failed with {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', errors='replace')}"
        )
    with tarfile.open(fileobj=BytesIO(proc.stdout), mode="r:") as archive:
        archive.extractall(destination, filter="data")
    source_root = destination / "src"
    if not (source_root / "csk" / "__init__.py").is_file():
        raise AssertionError(f"release archive did not contain src/csk: {commit}")
    return source_root


def run_cli(
    source_root: Path,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str | None],
) -> CLIOutput:
    """Run a CLI tree with LF-only capture and return its exact output bytes.

    The child redirects the real CLI into ``StringIO(newline="\\n")`` and
    writes a base64 JSON envelope through the binary pipe. Neither the host's
    locale nor Windows' text-stream newline conversion can rewrite CLI bytes.
    """
    merged_env = dict(os.environ)
    for key, value in env.items():
        if value is None:
            merged_env.pop(key, None)
        else:
            merged_env[key] = value
    inherited_pythonpath = merged_env.get("PYTHONPATH")
    merged_env["PYTHONPATH"] = os.pathsep.join(
        [str(source_root)] + ([inherited_pythonpath] if inherited_pythonpath else [])
    )
    proc = subprocess.run(
        [sys.executable, "-c", _CAPTURE_CLI_SCRIPT, *argv],
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"CLI child failed to execute with {proc.returncode}\n"
            f"stdout={proc.stdout.decode('utf-8', errors='replace')}\n"
            f"stderr={proc.stderr.decode('utf-8', errors='replace')}"
        )
    try:
        payload = json.loads(proc.stdout.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"CLI child returned invalid capture data: {proc.stdout!r}") from exc
    return CLIOutput(
        exit_code=int(payload["exit_code"]),
        stdout=base64.b64decode(payload["stdout"], validate=True),
        stderr=base64.b64decode(payload["stderr"], validate=True),
        version=str(payload["version"]),
        terminal_width=int(payload["terminal_width"]),
    )


def render_argv(command: GoldenCommand, root: Path) -> list[str]:
    return [item.replace("{ROOT}", str(root)) for item in command.argv]


def expected_path_hash(project_path: Path) -> str:
    """Recompute the pinned v1 ``path_hash`` line value (stdlib only).

    ``project resolve`` prints ``stable_path_hash`` (sha1 of the resolved
    project path, first 4 hex digits). The value is deterministic per path
    but root-dependent, so goldens carry a placeholder while the replay
    substitutes this independent recomputation; a change to the pinned
    algorithm fails the comparison.
    """
    import hashlib

    resolved = str(project_path.resolve())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:4]


def tokenize_output(data: bytes, *, root: str, version: str) -> bytes:
    """Replace native, slash, and JSON-escaped fixture roots and the version."""
    tokenized = data
    root_forms = {
        root,
        root.replace("\\", "/"),
        json.dumps(root)[1:-1],
        json.dumps(root.replace("\\", "/"))[1:-1],
    }
    for root_form in sorted(root_forms, key=len, reverse=True):
        tokenized = tokenized.replace(
            root_form.encode("utf-8"), GOLDEN_ROOT_TOKEN.encode("utf-8")
        )
    tokenized = tokenize_path_hash_line(tokenized, Path(root) / "project")
    return tokenized.replace(version.encode("utf-8"), VERSION_TOKEN.encode("utf-8"))


def tokenize_path_hash_line(data: bytes, project_path: Path) -> bytes:
    """Replace one exact ``path_hash: <hex>`` line value with a placeholder."""
    line = f"path_hash: {expected_path_hash(project_path)}".encode("utf-8")
    return data.replace(line, f"path_hash: {PATH_HASH_TOKEN}".encode("utf-8"))


def detokenize_output(data: bytes, *, root: str, version: str) -> bytes:
    """Restore placeholders to this run's root and version stamp."""
    restored = data.replace(
        GOLDEN_ROOT_TOKEN.encode("utf-8"), root.encode("utf-8")
    )
    restored = restored.replace(
        f"path_hash: {PATH_HASH_TOKEN}".encode("utf-8"),
        f"path_hash: {expected_path_hash(Path(root) / 'project')}".encode("utf-8"),
    )
    return restored.replace(VERSION_TOKEN.encode("utf-8"), version.encode("utf-8"))
