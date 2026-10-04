from __future__ import annotations

import importlib.util
import os
import base64
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from git_fixture_isolation import check_own_git_dir, discover_git_dir, scrub_git_child_env


# .scripts/release.sh is a POSIX (zsh) maintainer script; only tests that invoke
# it are skipped on Windows. Pure-Python release_support.py tests still run there.
RUNS_POSIX_RELEASE_SCRIPT = pytest.mark.skipif(
    os.name == "nt", reason=".scripts/release.sh is a POSIX maintainer script"
)

ROOT = Path(__file__).parents[1]
SCRIPT = Path(os.environ.get("CSK_RELEASE_SCRIPT", ROOT / ".scripts" / "release.sh"))
SUPPORT_FILE = Path(
    os.environ.get("CSK_RELEASE_SUPPORT", ROOT / ".scripts" / "release_support.py")
)
FIXTURES = ROOT / "tests" / "fixtures"
SUPPORT_SPEC = importlib.util.spec_from_file_location("release_support", SUPPORT_FILE)
assert SUPPORT_SPEC is not None and SUPPORT_SPEC.loader is not None
release_support = importlib.util.module_from_spec(SUPPORT_SPEC)
sys.modules[SUPPORT_SPEC.name] = release_support
SUPPORT_SPEC.loader.exec_module(release_support)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Fixture git children never inherit repository discovery: an ambient
    # GIT_DIR/GIT_WORK_TREE once redirected a fixture's config writes into
    # the enclosing checkout (BUG-261004-473myt).
    env = scrub_git_child_env(dict(os.environ), repo)
    return subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False, env=env
    )


def _assert_own_repo(repo: Path) -> None:
    """Prove the discovered repository is this fixture's own before config writes."""
    check_own_git_dir(repo, discover_git_dir(repo))


def test_union_merge_keeps_both_branches_appends_for_changelog_and_logbook(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "append-only-merge"
    repo.mkdir()
    init = _git(repo, "init", "--initial-branch=main", "--quiet")
    assert init.returncode == 0, init.stderr
    _assert_own_repo(repo)
    assert _git(repo, "config", "user.name", "Release Test").returncode == 0
    assert _git(repo, "config", "user.email", "release-test@example.invalid").returncode == 0

    attributes_source = Path(os.environ.get("CSK_GITATTRIBUTES", ROOT / ".gitattributes"))
    (repo / ".gitattributes").write_text(attributes_source.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("# Changelog\nBase release note\n", encoding="utf-8")
    (repo / "LOGBOOK.md").write_text("# Logbook\nBase entry\n", encoding="utf-8")
    assert _git(repo, "add", ".gitattributes", "CHANGELOG.md", "LOGBOOK.md").returncode == 0
    assert _git(repo, "commit", "--quiet", "-m", "base").returncode == 0
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert _git(repo, "switch", "--quiet", "-c", "append-a").returncode == 0
    for name in ("CHANGELOG.md", "LOGBOOK.md"):
        with (repo / name).open("a", encoding="utf-8") as stream:
            stream.write("Branch A append\n")
    assert _git(repo, "commit", "--quiet", "-am", "append A").returncode == 0

    assert _git(repo, "switch", "--quiet", "main").returncode == 0
    assert _git(repo, "switch", "--quiet", "-c", "append-b", base).returncode == 0
    for name in ("CHANGELOG.md", "LOGBOOK.md"):
        with (repo / name).open("a", encoding="utf-8") as stream:
            stream.write("Branch B append\n")
    assert _git(repo, "commit", "--quiet", "-am", "append B").returncode == 0

    assert _git(repo, "switch", "--quiet", "append-a").returncode == 0
    merged = _git(repo, "merge", "--no-edit", "append-b")
    assert merged.returncode == 0, merged.stderr
    for name in ("CHANGELOG.md", "LOGBOOK.md"):
        result = (repo / name).read_text(encoding="utf-8")
        assert "Branch A append" in result
        assert "Branch B append" in result


def test_narrowing_mutant_that_drops_changelog_union_is_killed_by_merge_test(
    tmp_path: Path,
) -> None:
    source = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    strict_rule = "CHANGELOG.md merge=union\n"
    assert strict_rule in source
    mutant = tmp_path / ".gitattributes"
    mutant.write_text(source.replace(strict_rule, "", 1), encoding="utf-8")
    run = _run_nested_release_test(
        tmp_path,
        "test_union_merge_keeps_both_branches_appends_for_changelog_and_logbook",
        {"CSK_GITATTRIBUTES": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_union_merge_keeps_both_branches_appends_for_changelog_and_logbook" in run.stdout
    assert "1 failed" in run.stdout


def test_narrowing_mutant_that_drops_logbook_union_is_killed_by_merge_test(
    tmp_path: Path,
) -> None:
    source = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    strict_rule = "LOGBOOK.md merge=union\n"
    assert strict_rule in source
    mutant = tmp_path / ".gitattributes"
    mutant.write_text(source.replace(strict_rule, "", 1), encoding="utf-8")
    run = _run_nested_release_test(
        tmp_path,
        "test_union_merge_keeps_both_branches_appends_for_changelog_and_logbook",
        {"CSK_GITATTRIBUTES": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_union_merge_keeps_both_branches_appends_for_changelog_and_logbook" in run.stdout
    assert "1 failed" in run.stdout


def test_cut_changelog_moves_unreleased_content_under_dated_version() -> None:
    source = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Added\n\n"
        "- A release note.\n\n"
        "## [0.18.3] - 2026-09-29\n\n"
        "- Previous release.\n"
    )

    changed = release_support.cut_changelog_text(source, "0.18.4", "2026-09-29")

    assert "## [Unreleased]\n\n## [0.18.4] - 2026-09-29\n\n### Added" in changed
    assert "- A release note." in changed
    assert changed.endswith("## [0.18.3] - 2026-09-29\n\n- Previous release.\n")


def test_cut_changelog_refuses_duplicate_version() -> None:
    with pytest.raises(ValueError, match="already contains version 0.18.3"):
        release_support.cut_changelog_text(
            "## [Unreleased]\n\n## [0.18.3] - 2026-09-29\n",
            "0.18.3",
            "2026-09-29",
        )


def _write_executable(file_path: Path, content: str) -> None:
    file_path.write_text(content, encoding="utf-8")
    file_path.chmod(0o755)


def _write_gh_stub(bin_directory: Path) -> Path:
    """Write a gh stub that validates each command's own option grammar."""
    stub = bin_directory / "gh"
    source = textwrap.dedent(
        """
        #!/usr/bin/env python3
        import json
        import os
        import sys

        def refuse(message):
            print(message, file=sys.stderr)
            raise SystemExit(97)

        def parse(raw, *, values=(), switches=(), positions=(0, 0)):
            parsed_values = {}
            parsed_switches = set()
            positional = []
            index = 0
            while index < len(raw):
                item = raw[index]
                if item in values:
                    if item in parsed_values or index + 1 >= len(raw):
                        refuse(f"invalid or repeated option: {item}")
                    parsed_values[item] = raw[index + 1]
                    index += 2
                    continue
                if item in switches:
                    if item in parsed_switches:
                        refuse(f"repeated option: {item}")
                    parsed_switches.add(item)
                    index += 1
                    continue
                if item.startswith("-"):
                    refuse(f"unknown flag for gh {group} {command}: {item}")
                positional.append(item)
                index += 1
            if not positions[0] <= len(positional) <= positions[1]:
                refuse(f"unexpected positional arguments for gh {group} {command}")
            return positional, parsed_values, parsed_switches

        def require(values, switches=()):
            missing = [item for item in values if item not in parsed_values]
            missing += [item for item in switches if item not in parsed_switches]
            if missing:
                refuse(f"missing option: {missing[0]}")

        arguments = sys.argv[1:]
        if not arguments:
            refuse("missing gh command")
        group = arguments.pop(0)
        command = arguments.pop(0) if arguments else ""

        with open(os.environ["CSK_STUB_CALLS"], "a", encoding="utf-8") as stream:
            stream.write("gh " + " ".join([group, command, *arguments]) + "\\n")

        parsed_values = {}
        parsed_switches = set()
        positional = []
        if group == "pr" and command == "create":
            positional, parsed_values, parsed_switches = parse(
                arguments,
                values=("--base", "--head", "--title", "--body", "--repo"),
            )
            require(("--base", "--head", "--title", "--body", "--repo"))
        elif group == "pr" and command == "view":
            positional, parsed_values, parsed_switches = parse(
                arguments, values=("--repo", "--json", "--jq"), positions=(1, 1)
            )
            require(("--repo", "--json", "--jq"))
        elif group == "pr" and command == "checks":
            positional, parsed_values, parsed_switches = parse(
                arguments,
                values=("--repo", "--interval", "--json", "--jq"),
                switches=("--required", "--watch"),
                positions=(1, 1),
            )
            require(("--repo",), ("--required",))
            if "--watch" in parsed_switches:
                require(("--interval",))
            elif "--json" in parsed_values:
                require(("--json", "--jq"))
            else:
                refuse("unsupported gh pr checks option set")
        elif group == "pr" and command == "review":
            positional, parsed_values, parsed_switches = parse(
                arguments,
                values=("--repo", "--body"),
                switches=("--comment",),
                positions=(1, 1),
            )
            require(("--repo", "--body"), ("--comment",))
        elif group == "run" and command == "list":
            positional, parsed_values, parsed_switches = parse(
                arguments,
                values=("--repo", "--workflow", "--limit", "--json"),
            )
            require(("--repo", "--workflow", "--limit", "--json"))
            if parsed_values["--json"] != "databaseId,event,headBranch,headSha,createdAt,startedAt,url":
                refuse("unexpected gh run list JSON fields")
        elif group == "run" and command == "watch":
            positional, parsed_values, parsed_switches = parse(
                arguments,
                values=("--repo", "--interval"),
                switches=("--exit-status",),
                positions=(1, 1),
            )
            require(("--repo", "--interval"), ("--exit-status",))
        elif group == "run" and command == "view":
            positional, parsed_values, parsed_switches = parse(
                arguments,
                values=("--repo", "--json", "--jq"),
                positions=(1, 1),
            )
            require(("--repo", "--json"))
            if parsed_values["--json"] != "jobs" or "--jq" in parsed_values:
                refuse("unsupported gh run view option set")
        elif group == "run" and command == "rerun":
            positional, parsed_values, parsed_switches = parse(
                arguments,
                values=("--repo",),
                switches=("--failed",),
                positions=(1, 1),
            )
            require(("--repo",), ("--failed",))
        elif group == "api":
            api_arguments = [command, *arguments]
            positional, parsed_values, parsed_switches = parse(
                api_arguments, values=("--jq",), positions=(1, 1)
            )
            if not positional[0].startswith("repos/"):
                refuse("unexpected gh api endpoint")
            require(("--jq",))
        elif group == "workflow" and command == "list":
            positional, parsed_values, parsed_switches = parse(
                arguments,
                values=("--jq", "--json", "--limit", "--repo", "--template"),
                switches=("--all",),
            )
        else:
            refuse(f"unsupported gh command: {group} {command}")

        if group == "pr" and command == "create":
            print("https://github.com/ivanopcode/cocoaskills/pull/42")
        elif group == "pr" and command == "view":
            print(os.environ.get("CSK_CUT_HEAD", ""))
        elif group == "pr" and command == "checks":
            raise SystemExit(int(os.environ.get("CSK_PR_CHECKS_STATUS", "0")))
        elif group == "run" and command == "list":
            workflow = parsed_values["--workflow"]
            fixture_name = "CSK_RELEASE_RUNS_JSON" if workflow == "release.yml" else "CSK_SMOKE_RUNS_JSON"
            fixture = os.environ.get(fixture_name)
            if fixture is not None:
                print(fixture)
            elif workflow == "release.yml":
                print(json.dumps([{
                    "databaseId": int(os.environ.get("CSK_RELEASE_RUN_ID", "101")),
                    "event": "push",
                    "headBranch": "v9.8.7",
                    "headSha": os.environ.get("CSK_CUT_HEAD", ""),
                    "createdAt": "2026-09-29T10:00:00Z",
                    "startedAt": "2026-09-29T10:01:00Z",
                }]))
            else:
                print(json.dumps([{
                    "databaseId": int(os.environ.get("CSK_SMOKE_RUN_ID", "202")),
                    "event": "workflow_run",
                    "headBranch": "main",
                    "headSha": os.environ.get("CSK_CUT_HEAD", ""),
                    "createdAt": "2026-09-29T10:02:00Z",
                    "startedAt": "2026-09-29T10:02:30Z",
                }]))
        elif group == "run" and command == "watch":
            run_id = positional[0]
            status_name = (
                "CSK_RELEASE_WATCH_STATUS"
                if run_id == os.environ.get("CSK_RELEASE_RUN_ID", "101")
                else "CSK_SMOKE_WATCH_STATUS"
            )
            raise SystemExit(int(os.environ.get(status_name, "0")))
        elif group == "run" and command == "view":
            run_id = positional[0]
            is_release_run = run_id == os.environ.get("CSK_RELEASE_RUN_ID", "101")
            jobs_status_name = (
                "CSK_RELEASE_JOBS_STATUS" if is_release_run else "CSK_SMOKE_JOBS_STATUS"
            )
            jobs_status = int(os.environ.get(jobs_status_name, "0"))
            if jobs_status:
                raise SystemExit(jobs_status)
            jobs_json_name = "CSK_RELEASE_JOBS_JSON" if is_release_run else "CSK_SMOKE_JOBS_JSON"
            print(os.environ.get(
                jobs_json_name,
                json.dumps({"jobs": [{
                    "name": "pipx / macos",
                    "conclusion": "failure",
                    "url": "https://github.com/ivanopcode/cocoaskills/actions/runs/202/job/3001",
                }]}),
            ))
        elif group == "run" and command == "rerun":
            raise SystemExit(0)
        elif group == "api":
            endpoint = sys.argv[2]
            if endpoint.endswith("/contents/Formula/cocoaskills.rb"):
                print(os.environ.get("CSK_FORMULA_B64", ""))
            elif "/commits?path=Formula/cocoaskills.rb" in endpoint:
                print(os.environ.get("CSK_TAP_COMMIT", ""))
            else:
                refuse("unexpected gh api endpoint")
        elif group == "workflow" and command == "list":
            print("[]")
        """
    ).lstrip()
    _write_executable(stub, source)
    return stub


def test_gh_stub_validates_flags_per_real_subcommand(tmp_path: Path) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    stub = _write_gh_stub(bin_directory)
    calls_file = tmp_path / "gh-calls.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": os.pathsep.join((str(bin_directory), os.environ.get("PATH", ""))),
            "CSK_STUB_CALLS": str(calls_file),
            "CSK_CUT_HEAD": "b" * 40,
        }
    )

    valid_calls = (
        ("api", "repos/ivanopcode/homebrew-csk/contents/Formula/cocoaskills.rb", "--jq", ".content"),
        ("pr", "view", "42", "--repo", "ivanopcode/cocoaskills", "--json", "headRefOid", "--jq", ".headRefOid"),
        ("run", "list", "--repo", "ivanopcode/cocoaskills", "--workflow", "release.yml", "--limit", "50", "--json", "databaseId,event,headBranch,headSha,createdAt,startedAt,url"),
        ("run", "view", "202", "--repo", "ivanopcode/cocoaskills", "--json", "jobs"),
        ("workflow", "list", "--repo", "ivanopcode/cocoaskills", "--all", "--limit", "50", "--json", "id,name,path,state"),
    )
    for arguments in valid_calls:
        result = subprocess.run(
            [sys.executable, str(stub), *arguments],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (arguments, result.stderr)

    invalid_calls = (
        ("api", "--repo", "ivanopcode/homebrew-csk", "repos/ivanopcode/homebrew-csk/commits", "--jq", ".[0].sha"),
        ("pr", "view", "42", "--not-a-gh-pr-flag"),
        ("run", "list", "--not-a-gh-run-flag"),
        ("workflow", "list", "--not-a-gh-workflow-flag"),
    )
    for arguments in invalid_calls:
        result = subprocess.run(
            [sys.executable, str(stub), *arguments],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 97, (arguments, result.stdout, result.stderr)
        assert "unknown flag" in result.stderr


def _run_nested_release_test(
    tmp_path: Path, test_name: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(environment)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{Path(__file__).resolve()}::{test_name}",
            f"--basetemp={tmp_path / 'nested-basetemp'}",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    (tmp_path / "nested-pytest.log").write_text(
        run.stdout + run.stderr, encoding="utf-8"
    )
    return run


def _seed_real_git_push_repositories(
    tmp_path: Path, project: Path
) -> tuple[str, Path, Path]:
    origin = tmp_path / "origin.git"
    wildberries = tmp_path / "wildberries.git"
    for bare_repository in (origin, wildberries):
        result = subprocess.run(
            ["git", "init", "--bare", "--quiet", "--initial-branch=main", str(bare_repository)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    init = _git(project, "init", "--initial-branch=main", "--quiet")
    assert init.returncode == 0, init.stderr
    _assert_own_repo(project)
    for key, value in (
        ("user.name", "Release Test"),
        ("user.email", "release-test@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        result = _git(project, "config", key, value)
        assert result.returncode == 0, result.stderr
    result = _git(project, "remote", "add", "origin", str(origin))
    assert result.returncode == 0, result.stderr

    (project / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.18.3] - 2026-09-29\n",
        encoding="utf-8",
    )
    result = _git(project, "add", "CHANGELOG.md")
    assert result.returncode == 0, result.stderr
    result = _git(project, "commit", "--quiet", "-m", "base")
    assert result.returncode == 0, result.stderr
    base_head = _git(project, "rev-parse", "HEAD").stdout.strip()
    result = _git(
        project,
        "push",
        "--quiet",
        "origin",
        "refs/heads/main:refs/heads/main",
    )
    assert result.returncode == 0, result.stderr

    result = _git(project, "switch", "--quiet", "-c", "release/v9.8.7")
    assert result.returncode == 0, result.stderr
    (project / "CUT_MARKER.md").write_text("prebuilt release head\n", encoding="utf-8")
    result = _git(project, "add", "CUT_MARKER.md")
    assert result.returncode == 0, result.stderr
    result = _git(project, "commit", "--quiet", "-m", "docs: cut 9.8.7 changelog")
    assert result.returncode == 0, result.stderr
    cut_head = _git(project, "rev-parse", "HEAD").stdout.strip()
    for tag_name, message in (
        ("v9.8.7", "CocoaSkills release"),
        ("unrelated-private-tag", "Unrelated annotated tag"),
    ):
        result = _git(project, "tag", "--annotate", tag_name, "--message", message, cut_head)
        assert result.returncode == 0, result.stderr

    result = _git(project, "config", "push.followTags", "true")
    assert result.returncode == 0, result.stderr
    source = SCRIPT.read_text(encoding="utf-8")
    remote_prefix = 'readonly WB_REMOTE="'
    remote_line = next(
        line for line in source.splitlines() if line.startswith(remote_prefix)
    )
    wb_remote = remote_line.removeprefix(remote_prefix).removesuffix('"')
    result = _git(
        project,
        "config",
        f"url.{wildberries.as_uri()}.insteadOf",
        wb_remote,
    )
    assert result.returncode == 0, result.stderr
    return cut_head, origin, wildberries


def _real_remote_refs(remote: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "ls-remote", "--refs", str(remote)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {
        reference: object_id
        for object_id, reference in (line.split("\t", 1) for line in result.stdout.splitlines())
    }


def _dry_run(
    tmp_path: Path,
    *,
    gate_status: int = 0,
    include_delivery: bool = True,
    script: Path = SCRIPT,
    evidence_mode: str = "valid",
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    calls_file = tmp_path / "stub-calls.log"
    notes_file = tmp_path / "Russian release notes.md"
    notes_file.write_text("Выпуск CocoaSkills\n", encoding="utf-8")
    head = "a" * 40

    _write_executable(
        bin_directory / "git",
        "#!/bin/bash\n"
        "printf 'git %s\\n' \"$*\" >> \"$CSK_STUB_CALLS\"\n"
        "case \" $* \" in\n"
        "  *'rev-parse --show-toplevel'*) printf '%s\\n' \"$CSK_PROJECT_ROOT\" ;;\n"
        "  *'rev-parse HEAD'*) printf '%s\\n' \"$CSK_TEST_HEAD\" ;;\n"
        "  *) exit 97 ;;\n"
        "esac\n",
    )
    for executable in ("gh", "glab"):
        _write_executable(
            bin_directory / executable,
            "#!/bin/bash\n"
            f"printf '{executable} %s\\n' \"$*\" >> \"$CSK_STUB_CALLS\"\n"
            "exit 97\n",
        )
    if include_delivery:
        _write_executable(
            bin_directory / "delivery",
            "#!/bin/bash\n"
            "printf 'delivery %s\\n' \"$*\" >> \"$CSK_STUB_CALLS\"\n"
            "case \"$CSK_DELIVERY_EVIDENCE_MODE\" in\n"
            "  valid) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v1\\\",\\\"command\\\":\\\"trunk-verify\\\",\\\"verdict\\\":\\\"green\\\",\\\"exit_code\\\":0,\\\"head\\\":\\\"%s\\\",\\\"reason\\\":{\\\"message\\\":\\\"%s\\\"}}\\n' \"$CSK_TEST_HEAD\" \"$CSK_EXTERNAL_SECRET_SENTINEL\" ;;\n"
            "  *) printf '%s\\n' 'not-json' ;;\n"
            "esac\n"
            "exit \"$CSK_DELIVERY_EXIT\"\n",
        )
    (bin_directory / "python3").symlink_to(sys.executable)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_directory}:/bin:/usr/bin",
            "CSK_STUB_CALLS": str(calls_file),
            "CSK_PROJECT_ROOT": str(ROOT),
            "CSK_TEST_HEAD": head,
            "CSK_DELIVERY_EXIT": str(gate_status),
            "CSK_DELIVERY_EVIDENCE_MODE": evidence_mode,
            "CSK_EXTERNAL_SECRET_SENTINEL": "delivery-secret-sentinel",
            "GH_TOKEN": "github-secret-sentinel",
            "GITLAB_TOKEN": "gitlab-secret-sentinel",
        }
    )
    env.pop("DELIVERY_BIN", None)
    result = subprocess.run(
        [str(script), "9.8.7", "--wb-notes", str(notes_file), "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    calls = calls_file.read_text(encoding="utf-8") if calls_file.exists() else ""
    return result, calls, notes_file


@RUNS_POSIX_RELEASE_SCRIPT
def test_release_dry_run_calls_production_gate_and_prints_every_step(
    tmp_path: Path,
) -> None:
    result, calls, notes_file = _dry_run(tmp_path, gate_status=0)

    assert result.returncode == 0, result.stderr
    assert "Release version: 9.8.7" in result.stdout
    assert "Release tag: v9.8.7" in result.stdout
    assert f"Wildberries notes file: {notes_file}" in result.stdout
    assert "delivery trunk-verify --head " + "a" * 40 in result.stdout
    assert f'git tag -s v9.8.7 -m "CocoaSkills 9.8.7" <reviewed-cut-head>' in result.stdout
    assert f'Dry-run probe head: {"a" * 40}' in result.stdout
    assert "Live: watch release.yml" in result.stdout
    assert "Wildberries mirror" in result.stdout
    assert "GitLab release" in result.stdout
    assert "distribution-smoke.yml" in result.stdout
    assert "Verify PyPI JSON, the Homebrew tap formula and latest commit" in result.stdout
    assert (
        'git push --no-follow-tags --set-upstream origin '
        '"refs/heads/release/v9.8.7:refs/heads/release/v9.8.7"'
    ) in result.stdout
    assert (
        'git push --no-follow-tags origin "<reviewed-cut-head>:refs/heads/main"'
    ) in result.stdout
    assert (
        'git push --no-follow-tags origin '
        '"refs/tags/v9.8.7:refs/tags/v9.8.7"'
    ) in result.stdout
    assert (
        'git push --no-follow-tags '
        '"git@gitlab.wildberries.ru:portals/agentic-infra/cocoaskills.git" '
        '"<reviewed-cut-head>:refs/heads/main" '
        '"refs/tags/v9.8.7:refs/tags/v9.8.7"'
    ) in result.stdout
    assert "delivery trunk-verify --head " + "a" * 40 in calls
    assert "git tag" not in calls
    assert "git push" not in calls
    assert "gh " not in calls
    assert "glab " not in calls
    for secret in (
        "delivery-secret-sentinel",
        "github-secret-sentinel",
        "gitlab-secret-sentinel",
    ):
        assert secret not in result.stdout
        assert secret not in result.stderr


@RUNS_POSIX_RELEASE_SCRIPT
def test_release_dry_run_refuses_exit_1_before_tag(tmp_path: Path) -> None:
    result, calls, _ = _dry_run(tmp_path, gate_status=1)

    assert result.returncode == 1
    assert "trunk-verify returned red" in result.stderr
    assert "Watch distribution-smoke.yml" in result.stdout
    assert "Verify PyPI JSON" in result.stdout
    assert "Run git tag -s" not in result.stdout
    assert "git tag" not in calls
    assert "delivery trunk-verify --head " + "a" * 40 in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_release_dry_run_refuses_exit_2_before_tag(tmp_path: Path) -> None:
    result, calls, _ = _dry_run(tmp_path, gate_status=2)

    assert result.returncode == 2
    assert "trunk-verify returned unknown" in result.stderr
    assert "Run git tag -s" not in result.stdout
    assert "git tag" not in calls
    assert "delivery trunk-verify --head " + "a" * 40 in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_release_dry_run_refuses_when_delivery_binary_is_missing_before_tag(
    tmp_path: Path,
) -> None:
    result, calls, _ = _dry_run(tmp_path, include_delivery=False)

    assert result.returncode == 2
    assert "delivery trunk-verify is unavailable" in result.stderr
    assert "Run git tag -s" not in result.stdout
    assert "delivery trunk-verify" not in calls
    assert "git tag" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_admits_red_trunk_is_killed_by_behavioral_test(
    tmp_path: Path,
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    strict_case = '  case "$verify_status" in\n    0)\n'
    assert strict_case in source
    mutant = tmp_path / "release-mutant.sh"
    mutant.write_text(source.replace(strict_case, '  case "$verify_status" in\n    0|1)\n', 1), encoding="utf-8")
    mutant.chmod(0o755)
    mutant_log = tmp_path / "narrowing-mutant-pytest.log"

    env = os.environ.copy()
    env["CSK_RELEASE_SCRIPT"] = str(mutant)
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{Path(__file__).resolve()}::test_release_dry_run_refuses_exit_1_before_tag",
            f"--basetemp={tmp_path / 'mutant-basetemp'}",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    mutant_log.write_text(run.stdout + run.stderr, encoding="utf-8")

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_release_dry_run_refuses_exit_1_before_tag" in run.stdout
    assert "1 failed" in run.stdout


def test_workflow_run_selector_uses_exact_head_and_release_start_time() -> None:
    release_rows = json.loads((FIXTURES / "real-release-run-list.json").read_text())
    smoke_rows = json.loads(
        (FIXTURES / "real-distribution-smoke-run-list.json").read_text()
    )
    release_id, release_started_at = release_support.select_workflow_run(
        json.dumps(release_rows), "push", release_rows[0]["headSha"]
    )
    smoke_id, _ = release_support.select_workflow_run(
        json.dumps(smoke_rows),
        "workflow_run",
        release_rows[0]["headSha"],
        release_started_at,
    )

    assert release_id == str(release_rows[0]["databaseId"])
    assert smoke_id == str(smoke_rows[0]["databaseId"])
    assert smoke_rows[0]["headBranch"] == "main"
    assert smoke_rows[0]["createdAt"] > release_started_at


def test_failed_job_rows_requires_same_run_links_for_every_failed_job() -> None:
    jobs = {
        "jobs": [
            {
                "name": "pipx / macos",
                "conclusion": "failure",
                "url": "https://github.com/ivanopcode/cocoaskills/actions/runs/202/job/3001",
            },
            {
                "name": "mise / ubuntu",
                "conclusion": "failure",
                "url": "https://github.com/ivanopcode/cocoaskills/actions/runs/202/job/3002",
            },
        ]
    }
    assert release_support.failed_job_rows(json.dumps(jobs), "202") == [
        ("pipx / macos", "https://github.com/ivanopcode/cocoaskills/actions/runs/202/job/3001"),
        ("mise / ubuntu", "https://github.com/ivanopcode/cocoaskills/actions/runs/202/job/3002"),
    ]
    jobs["jobs"][1]["url"] = "https://github.com/ivanopcode/cocoaskills/actions/runs/203/job/3002"
    with pytest.raises(ValueError, match="URL is missing or mismatched"):
        release_support.failed_job_rows(json.dumps(jobs), "202")


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_reports_smoke_failure_and_manual_rerun_without_retry(
    tmp_path: Path,
) -> None:
    result, calls, _ = _live_release(tmp_path, smoke_watch_status=1)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "The release itself has already been published" in result.stderr
    assert "only distribution verification failed" in result.stderr
    assert "pipx / macos: https://github.com/ivanopcode/cocoaskills/actions/runs/202/job/3001" in result.stderr
    assert "mise / ubuntu: https://github.com/ivanopcode/cocoaskills/actions/runs/202/job/3002" in result.stderr
    assert "gh run rerun 202 --failed" in result.stderr
    assert ".scripts/release.sh 9.8.7 --resume-from verify" in result.stderr
    assert "gh run rerun" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_auto_reruns_one_smoke_run_is_killed_by_live_test(
    tmp_path: Path,
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    strict_manual_only = (
        '  if report_smoke_failure "$run_id"; then\n'
        "    return 1\n"
        "  fi\n"
        "  return 2\n"
    )
    assert strict_manual_only in source
    mutant = tmp_path / "release-mutant.sh"
    mutant.write_text(
        source.replace(
            strict_manual_only,
            '  if [[ "$run_id" == "202" ]]; then\n'
            '    quiet_external gh run rerun "$run_id" --failed >/dev/null\n'
            "    return 1\n"
            "  fi\n"
            + strict_manual_only,
            1,
        ),
        encoding="utf-8",
    )
    mutant.chmod(0o755)
    run = _run_nested_release_test(
        tmp_path,
        "test_live_release_reports_smoke_failure_and_manual_rerun_without_retry",
        {"CSK_RELEASE_SCRIPT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_live_release_reports_smoke_failure_and_manual_rerun_without_retry" in run.stdout
    assert "1 failed" in run.stdout


def test_final_version_helpers_require_the_requested_homebrew_and_mise_version() -> None:
    formula = (
        '  url "https://github.com/ivanopcode/cocoaskills/releases/download/'
        'v0.18.4/cocoaskills-0.18.4.tar.gz"\n'
    )
    assert release_support.homebrew_formula_has_version(formula, "0.18.4")
    assert not release_support.homebrew_formula_has_version(formula, "0.18.5")
    assert release_support.mise_output_has_version("0.18.3\n0.18.4\n", "0.18.4")
    assert not release_support.mise_output_has_version("0.18.3\n", "0.18.4")


def _live_release(
    tmp_path: Path,
    *,
    gate_status: int = 0,
    include_delivery: bool = True,
    release_watch_status: int = 0,
    release_jobs_status: int = 0,
    smoke_watch_status: int = 0,
    smoke_jobs_status: int = 0,
    status_read_exit: int = 0,
    evidence_mode: str = "valid",
    cut_head: str = "b" * 40,
    release_runs_json: str | None = None,
    smoke_runs_json: str | None = None,
    resume: bool = False,
    real_git_pushes: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    project = tmp_path / "project"
    scripts = project / ".scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / "release.sh")
    shutil.copy2(SUPPORT_FILE, scripts / "release_support.py")
    (scripts / "release.sh").chmod(0o755)
    if real_git_pushes:
        cut_head, _origin_repository, _wildberries_repository = (
            _seed_real_git_push_repositories(tmp_path, project)
        )
    else:
        (project / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n## [0.18.3] - 2026-09-29\n",
            encoding="utf-8",
        )
    notes_file = tmp_path / "wb-notes.md"
    notes_file.write_text("Релиз CocoaSkills\n", encoding="utf-8")
    signing_public_key = project / "signing.pub"
    signing_public_key.write_text("ssh-ed25519 test-key\n", encoding="utf-8")
    allowed_signers = project / "allowed-signers"
    allowed_signers.write_text("release-test ssh-ed25519 test-key\n", encoding="utf-8")

    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    calls_file = tmp_path / "live-stub-calls.log"
    committed_marker = tmp_path / "commit-created"
    main_marker = tmp_path / "main-fast-forwarded"
    if real_git_pushes:
        base_head = _git(project, "rev-parse", "refs/heads/main").stdout.strip()
    else:
        base_head = "a" * 40
    tag_object = "e" * 40
    remote_tag_exists = int(resume)
    release_run_id = "101"
    smoke_run_id = "202"
    if release_runs_json is not None:
        release_run_id = str(json.loads(release_runs_json)[0]["databaseId"])
    if smoke_runs_json is not None:
        smoke_run_id = str(json.loads(smoke_runs_json)[0]["databaseId"])
    tap_commit = "c" * 40
    formula = (
        '  url "https://github.com/ivanopcode/cocoaskills/releases/download/'
        'v9.8.7/cocoaskills-9.8.7.tar.gz"\n'
    )
    encoded_formula = base64.b64encode(formula.encode("utf-8")).decode("ascii")
    formula_b64 = "\n".join(textwrap.wrap(encoded_formula, 60))

    _write_executable(
        bin_directory / "git",
        "#!/bin/bash\n"
        "printf 'git %s\\n' \"$*\" >> \"$CSK_STUB_CALLS\"\n"
        "if [[ \"${1:-}\" == push && -n \"${CSK_REAL_PUSH_REPO:-}\" ]]; then\n"
        "  \"$CSK_REAL_GIT\" -C \"$CSK_REAL_PUSH_REPO\" \"$@\"\n"
        "  push_status=$?\n"
        "  if ((push_status == 0)) && [[ \" $* \" == *'refs/heads/main'* ]]; then : > \"$CSK_MAIN_MARKER\"; fi\n"
        "  exit \"$push_status\"\n"
        "fi\n"
        "case \" $* \" in\n"
        "  *'rev-parse --show-toplevel'*) printf '%s\\n' \"$CSK_PROJECT_ROOT\" ;;\n"
        "  *'remote get-url origin'*) printf '%s\\n' 'https://github.com/ivanopcode/cocoaskills' ;;\n"
        "  *'branch --show-current'*) printf '%s\\n' 'main' ;;\n"
        "  *'config --get gpg.format'*) printf '%s\\n' 'ssh' ;;\n"
        "  *'config --path --get user.signingkey'*) printf '%s\\n' \"$CSK_SIGNING_PUBLIC_KEY\" ;;\n"
        "  *'config --path --get gpg.ssh.allowedSignersFile'*) printf '%s\\n' \"$CSK_ALLOWED_SIGNERS\" ;;\n"
        "  *'rev-parse refs/tags/'*) if [[ \"$*\" == *'^{commit}'* ]]; then printf '%s\\n' \"$CSK_CUT_HEAD\"; else printf '%s\\n' \"$CSK_TAG_OBJECT\"; fi ;;\n"
        "  *'rev-parse HEAD'*) if [[ -f \"$CSK_COMMITTED_MARKER\" ]]; then printf '%s\\n' \"$CSK_CUT_HEAD\"; else printf '%s\\n' \"$CSK_BASE_HEAD\"; fi ;;\n"
        "  *'rev-parse refs/remotes/origin/main'*) if [[ -f \"$CSK_MAIN_MARKER\" ]]; then printf '%s\\n' \"$CSK_CUT_HEAD\"; else printf '%s\\n' \"$CSK_BASE_HEAD\"; fi ;;\n"
        "  *'show-ref --verify --quiet refs/tags/'*) exit 1 ;;\n"
        "  *'show-ref --verify --quiet refs/heads/'*) exit 1 ;;\n"
        "  *'ls-remote --tags origin refs/tags/'*) if [[ \"$CSK_REMOTE_TAG_EXISTS\" == 1 ]]; then printf '%s\\trefs/tags/v9.8.7\\n' \"$CSK_TAG_OBJECT\"; else exit 0; fi ;;\n"
        "  *'ls-remote --tags origin'*) exit 0 ;;\n"
        "  *'ls-remote --heads origin'*) exit 0 ;;\n"
        "  *'switch -c release/'*) exit 0 ;;\n"
        "  *'commit -S'*) : > \"$CSK_COMMITTED_MARKER\" ;;\n"
        "  *'verify-commit '*|*'verify-tag '*) exit 0 ;;\n"
        "  *'merge-base --is-ancestor'*) exit 0 ;;\n"
        "  *'push '*refs/heads/main*) : > \"$CSK_MAIN_MARKER\" ;;\n"
        "  *'status --porcelain'*) exit \"$CSK_STATUS_READ_EXIT\" ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )

    _write_gh_stub(bin_directory)
    _write_executable(
        bin_directory / "glab",
        "#!/bin/bash\nprintf 'glab %s\\n' \"$*\" >> \"$CSK_STUB_CALLS\"\nexit 0\n",
    )
    _write_executable(
        bin_directory / "ssh-add",
        "#!/bin/bash\nprintf '256 SHA256:test-key-fingerprint release-test (ED25519)\\n'\n",
    )
    _write_executable(
        bin_directory / "ssh-keygen",
        "#!/bin/bash\nprintf '256 SHA256:test-key-fingerprint release-test (ED25519)\\n'\n",
    )
    _write_executable(
        bin_directory / "curl",
        "#!/bin/bash\n"
        "printf 'curl %s\\n' \"$*\" >> \"$CSK_STUB_CALLS\"\n"
        "printf '%s\\n' '{\"info\":{\"version\":\"9.8.7\"}}'\n",
    )
    _write_executable(
        bin_directory / "mise",
        "#!/bin/bash\n"
        "printf 'mise %s\\n' \"$*\" >> \"$CSK_STUB_CALLS\"\n"
        "printf '%s\\n' '9.8.7'\n",
    )
    _write_executable(bin_directory / "sleep", "#!/bin/bash\nexit 0\n")
    if include_delivery:
        _write_executable(
            bin_directory / "delivery",
            "#!/bin/bash\n"
            "printf 'delivery %s\\n' \"$*\" >> \"$CSK_STUB_CALLS\"\n"
            "case \"$CSK_DELIVERY_EVIDENCE_MODE\" in\n"
            "  valid) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v1\\\",\\\"command\\\":\\\"trunk-verify\\\",\\\"verdict\\\":\\\"green\\\",\\\"exit_code\\\":0,\\\"head\\\":\\\"%s\\\",\\\"reason\\\":{\\\"message\\\":\\\"%s\\\"}}\\n' \"$3\" \"$CSK_EXTERNAL_SECRET_SENTINEL\" ;;\n"
            "  other-head) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v1\\\",\\\"command\\\":\\\"trunk-verify\\\",\\\"verdict\\\":\\\"green\\\",\\\"exit_code\\\":0,\\\"head\\\":\\\"%s\\\"}\\n' \"$CSK_OTHER_HEAD\" ;;\n"
            "  missing-head) printf '%s\\n' '{\"schema\":\"delivery.trunk-verify/v1\",\"command\":\"trunk-verify\",\"verdict\":\"green\",\"exit_code\":0}' ;;\n"
            "  missing-verdict) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v1\\\",\\\"command\\\":\\\"trunk-verify\\\",\\\"exit_code\\\":0,\\\"head\\\":\\\"%s\\\"}\\n' \"$3\" ;;\n"
            "  duplicate-head) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v1\\\",\\\"command\\\":\\\"trunk-verify\\\",\\\"verdict\\\":\\\"green\\\",\\\"exit_code\\\":0,\\\"head\\\":\\\"%s\\\",\\\"head\\\":\\\"%s\\\"}\\n' \"$3\" \"$CSK_OTHER_HEAD\" ;;\n"
            "  non-green) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v1\\\",\\\"command\\\":\\\"trunk-verify\\\",\\\"verdict\\\":\\\"red\\\",\\\"exit_code\\\":0,\\\"head\\\":\\\"%s\\\"}\\n' \"$3\" ;;\n"
            "  malformed) printf '%s\\n' '{invalid json' ;;\n"
            "  wrong-schema) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v9\\\",\\\"command\\\":\\\"trunk-verify\\\",\\\"verdict\\\":\\\"green\\\",\\\"exit_code\\\":0,\\\"head\\\":\\\"%s\\\"}\\n' \"$3\" ;;\n"
            "  wrong-command) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v1\\\",\\\"command\\\":\\\"other\\\",\\\"verdict\\\":\\\"green\\\",\\\"exit_code\\\":0,\\\"head\\\":\\\"%s\\\"}\\n' \"$3\" ;;\n"
            "  nonzero-document) printf '{\\\"schema\\\":\\\"delivery.trunk-verify/v1\\\",\\\"command\\\":\\\"trunk-verify\\\",\\\"verdict\\\":\\\"green\\\",\\\"exit_code\\\":1,\\\"head\\\":\\\"%s\\\"}\\n' \"$3\" ;;\n"
            "esac\n"
            "exit \"$CSK_DELIVERY_EXIT\"\n",
        )
    (bin_directory / "python3").symlink_to(sys.executable)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_directory}:/bin:/usr/bin",
            "CSK_STUB_CALLS": str(calls_file),
            "CSK_PROJECT_ROOT": str(project),
            "CSK_BASE_HEAD": base_head,
            "CSK_CUT_HEAD": cut_head,
            "CSK_TAG_OBJECT": tag_object,
            "CSK_REMOTE_TAG_EXISTS": str(remote_tag_exists),
            "CSK_COMMITTED_MARKER": str(committed_marker),
            "CSK_MAIN_MARKER": str(main_marker),
            "CSK_SIGNING_PUBLIC_KEY": str(signing_public_key),
            "CSK_ALLOWED_SIGNERS": str(allowed_signers),
            "CSK_DELIVERY_EXIT": str(gate_status),
            "CSK_DELIVERY_EVIDENCE_MODE": evidence_mode,
            "CSK_OTHER_HEAD": "d" * 40,
            "CSK_STATUS_READ_EXIT": str(status_read_exit),
            "CSK_RELEASE_RUN_ID": release_run_id,
            "CSK_SMOKE_RUN_ID": smoke_run_id,
            "CSK_RELEASE_WATCH_STATUS": str(release_watch_status),
            "CSK_SMOKE_WATCH_STATUS": str(smoke_watch_status),
            "CSK_RELEASE_JOBS_STATUS": str(release_jobs_status),
            "CSK_SMOKE_JOBS_STATUS": str(smoke_jobs_status),
            "CSK_RELEASE_JOBS_JSON": json.dumps(
                {
                    "jobs": [
                        {
                            "name": "release / ubuntu",
                            "conclusion": "failure",
                            "url": f"https://github.com/ivanopcode/cocoaskills/actions/runs/{release_run_id}/job/2001",
                        }
                    ]
                }
            ),
            "CSK_SMOKE_JOBS_JSON": json.dumps(
                {
                    "jobs": [
                        {
                            "name": "pipx / macos",
                            "conclusion": "failure",
                            "url": f"https://github.com/ivanopcode/cocoaskills/actions/runs/{smoke_run_id}/job/3001",
                        },
                        {
                            "name": "mise / ubuntu",
                            "conclusion": "failure",
                            "url": f"https://github.com/ivanopcode/cocoaskills/actions/runs/{smoke_run_id}/job/3002",
                        }
                    ]
                }
            ),
            "CSK_EXTERNAL_SECRET_SENTINEL": "delivery-secret-sentinel",
            "GH_TOKEN": "github-secret-sentinel",
            "GITLAB_TOKEN": "gitlab-secret-sentinel",
            "CSK_FORMULA_B64": formula_b64,
            "CSK_TAP_COMMIT": tap_commit,
        }
    )
    if real_git_pushes:
        real_git = shutil.which("git")
        assert real_git is not None
        env["CSK_REAL_GIT"] = real_git
        env["CSK_REAL_PUSH_REPO"] = str(project)
    if release_runs_json is not None:
        env["CSK_RELEASE_RUNS_JSON"] = release_runs_json
    if smoke_runs_json is not None:
        env["CSK_SMOKE_RUNS_JSON"] = smoke_runs_json
    env.pop("DELIVERY_BIN", None)
    if resume:
        release_args = [str(scripts / "release.sh"), "9.8.7", "--resume-from", "verify"]
    else:
        release_args = [str(scripts / "release.sh"), "9.8.7", "--wb-notes", str(notes_file)]
    result = subprocess.run(
        release_args,
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    calls = calls_file.read_text(encoding="utf-8") if calls_file.exists() else ""
    return result, calls, project


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_gates_the_exact_fast_forward_head_before_signing_tag(
    tmp_path: Path,
) -> None:
    result, calls, project = _live_release(tmp_path, gate_status=0)

    assert result.returncode == 0, result.stdout + result.stderr
    gate_call = f"delivery trunk-verify --head {'b' * 40}"
    assert gate_call in calls
    assert "git tag -s v9.8.7 -m CocoaSkills 9.8.7 " + "b" * 40 in calls
    assert calls.index(gate_call) < calls.index("git tag -s v9.8.7")
    assert "gh pr create --base main --head release/v9.8.7" in calls
    assert "gh pr review 42" in calls
    assert "git push --no-follow-tags origin " + "b" * 40 + ":refs/heads/main" in calls
    assert "glab release create v9.8.7" in calls
    assert "gh run rerun" not in calls
    assert "## [9.8.7] - " in (project / "CHANGELOG.md").read_text(encoding="utf-8")
    for secret in (
        "delivery-secret-sentinel",
        "github-secret-sentinel",
        "gitlab-secret-sentinel",
    ):
        assert secret not in result.stdout
        assert secret not in result.stderr


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_real_git_pushes_only_explicit_refs_with_follow_tags_enabled(
    tmp_path: Path,
) -> None:
    result, calls, project = _live_release(tmp_path, real_git_pushes=True)

    assert result.returncode == 0, result.stdout + result.stderr
    follow_tags = _git(project, "config", "--get", "push.followTags")
    assert follow_tags.returncode == 0
    assert follow_tags.stdout.strip() == "true"

    cut_head = _git(project, "rev-parse", "refs/heads/release/v9.8.7").stdout.strip()
    extra_tag_target = _git(
        project, "rev-parse", "refs/tags/unrelated-private-tag^{}"
    ).stdout.strip()
    assert extra_tag_target == cut_head

    origin_refs = _real_remote_refs(tmp_path / "origin.git")
    assert set(origin_refs) == {
        "refs/heads/main",
        "refs/heads/release/v9.8.7",
        "refs/tags/v9.8.7",
    }
    assert origin_refs["refs/heads/main"] == cut_head
    assert origin_refs["refs/heads/release/v9.8.7"] == cut_head
    assert origin_refs["refs/tags/v9.8.7"] == _git(
        project, "rev-parse", "refs/tags/v9.8.7"
    ).stdout.strip()

    wildberries_refs = _real_remote_refs(tmp_path / "wildberries.git")
    assert set(wildberries_refs) == {"refs/heads/main", "refs/tags/v9.8.7"}
    assert wildberries_refs["refs/heads/main"] == cut_head
    assert wildberries_refs["refs/tags/v9.8.7"] == origin_refs["refs/tags/v9.8.7"]

    push_calls = [line for line in calls.splitlines() if line.startswith("git push ")]
    assert len(push_calls) == 4
    assert all("--no-follow-tags" in line for line in push_calls)
    assert push_calls == [
        "git push --no-follow-tags --set-upstream origin "
        "refs/heads/release/v9.8.7:refs/heads/release/v9.8.7",
        f"git push --no-follow-tags origin {cut_head}:refs/heads/main",
        "git push --no-follow-tags origin "
        "refs/tags/v9.8.7:refs/tags/v9.8.7",
        "git push --no-follow-tags "
        "git@gitlab.wildberries.ru:portals/agentic-infra/cocoaskills.git "
        f"{cut_head}:refs/heads/main refs/tags/v9.8.7:refs/tags/v9.8.7",
    ]


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_real_git_does_not_follow_tags_before_trunk_verification(
    tmp_path: Path,
) -> None:
    result, calls, project = _live_release(
        tmp_path, gate_status=1, real_git_pushes=True
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "trunk-verify returned red" in result.stderr
    assert "git tag -s" not in calls

    origin_refs = _real_remote_refs(tmp_path / "origin.git")
    cut_head = _git(project, "rev-parse", "refs/heads/release/v9.8.7").stdout.strip()
    assert origin_refs == {
        "refs/heads/main": cut_head,
        "refs/heads/release/v9.8.7": cut_head,
    }
    assert not any(reference.startswith("refs/tags/") for reference in origin_refs)
    assert _real_remote_refs(tmp_path / "wildberries.git") == {}

    push_calls = [line for line in calls.splitlines() if line.startswith("git push ")]
    assert len(push_calls) == 2
    assert push_calls[0].startswith(
        "git push --no-follow-tags --set-upstream origin "
    )


@pytest.mark.parametrize(
    ("push_site", "push_prefix", "behavior_test"),
    [
        (
            "cut-branch",
            "if ! git push --no-follow-tags --set-upstream",
            "test_live_release_real_git_does_not_follow_tags_before_trunk_verification",
        ),
        (
            "reviewed-main",
            'if ! git push --no-follow-tags origin "${pr_head}:refs/heads/main"',
            "test_live_release_real_git_pushes_only_explicit_refs_with_follow_tags_enabled",
        ),
        (
            "release-tag",
            'if ! git push --no-follow-tags origin \\\n    "refs/tags/$release_tag:refs/tags/$release_tag"',
            "test_live_release_real_git_pushes_only_explicit_refs_with_follow_tags_enabled",
        ),
        (
            "wildberries-mirror",
            'if ! git push --no-follow-tags "$WB_REMOTE"',
            "test_live_release_real_git_pushes_only_explicit_refs_with_follow_tags_enabled",
        ),
    ],
    ids=("cut-branch", "reviewed-main", "release-tag", "wildberries-mirror"),
)
@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_restores_follow_tags_at_each_push_is_killed_by_real_git(
    tmp_path: Path,
    push_site: str,
    push_prefix: str,
    behavior_test: str,
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count(push_prefix) == 1, push_site
    mutant_source = source.replace(
        push_prefix, push_prefix.replace("--no-follow-tags ", "", 1), 1
    )
    mutant = tmp_path / "release-follow-tags-mutant.sh"
    mutant.write_text(mutant_source, encoding="utf-8")
    mutant.chmod(0o755)
    run = _run_nested_release_test(
        tmp_path,
        behavior_test,
        {"CSK_RELEASE_SCRIPT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert behavior_test in run.stdout
    assert "1 failed" in run.stdout


@RUNS_POSIX_RELEASE_SCRIPT
@pytest.mark.parametrize(("gate_status", "expected_code", "description"), [(1, 1, "red"), (2, 2, "unknown")])
def test_live_release_refuses_red_or_unknown_trunk_before_signing_tag(
    tmp_path: Path, gate_status: int, expected_code: int, description: str
) -> None:
    result, calls, _ = _live_release(tmp_path, gate_status=gate_status)

    assert result.returncode == expected_code
    assert f"trunk-verify returned {description}" in result.stderr
    assert f"delivery trunk-verify --head {'b' * 40}" in calls
    assert "git tag -s" not in calls


def _assert_trunk_evidence_refused_before_tag(
    result: subprocess.CompletedProcess[str], calls: str
) -> None:
    assert result.returncode == 2, result.stdout + result.stderr
    assert "trunk-verify evidence" in result.stderr
    assert "git tag -s" not in calls
    assert "git push --no-follow-tags origin refs/tags/v9.8.7" not in calls
    assert "git push --no-follow-tags git@gitlab.wildberries.ru" not in calls
    assert "glab release create" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_refuses_trunk_evidence_for_other_head_before_signing_tag(
    tmp_path: Path,
) -> None:
    result, calls, _ = _live_release(tmp_path, evidence_mode="other-head")

    _assert_trunk_evidence_refused_before_tag(result, calls)
    assert f"delivery trunk-verify --head {'b' * 40}" in calls


@RUNS_POSIX_RELEASE_SCRIPT
@pytest.mark.parametrize(
    "evidence_mode",
    (
        "malformed",
        "missing-head",
        "missing-verdict",
        "duplicate-head",
        "non-green",
        "wrong-schema",
        "wrong-command",
        "nonzero-document",
    ),
    ids=(
        "malformed-json",
        "missing-head",
        "missing-green-verdict",
        "duplicate-head",
        "red-verdict",
        "wrong-schema",
        "wrong-command",
        "document-exit-mismatch",
    ),
)
def test_live_release_refuses_malformed_missing_or_non_green_trunk_evidence(
    tmp_path: Path, evidence_mode: str
) -> None:
    result, calls, _ = _live_release(tmp_path, evidence_mode=evidence_mode)

    _assert_trunk_evidence_refused_before_tag(result, calls)


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_accepts_another_trunk_head_is_killed_by_live_test(
    tmp_path: Path,
) -> None:
    source = SUPPORT_FILE.read_text(encoding="utf-8")
    exact_head_check = '        and document.get("head") == expected_head\n'
    assert exact_head_check in source
    mutant = tmp_path / "release-support-mutant.py"
    mutant.write_text(
        source.replace(exact_head_check, "        and True\n", 1), encoding="utf-8"
    )
    run = _run_nested_release_test(
        tmp_path,
        "test_live_release_refuses_trunk_evidence_for_other_head_before_signing_tag",
        {"CSK_RELEASE_SUPPORT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_live_release_refuses_trunk_evidence_for_other_head_before_signing_tag" in run.stdout
    assert "1 failed" in run.stdout


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_refuses_failed_checkout_status_read_before_publication(
    tmp_path: Path,
) -> None:
    result, calls, _ = _live_release(tmp_path, status_read_exit=128)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot read checkout status" in result.stderr
    assert "git status --porcelain" in calls
    assert "git fetch origin" not in calls
    assert "gh pr create" not in calls
    assert "git push" not in calls
    assert "glab release create" not in calls
    assert "delivery trunk-verify" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_accepts_empty_failed_status_read_is_killed(
    tmp_path: Path,
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    strict_read = (
        '  if dirty_state="$(git status --porcelain --untracked-files=normal)"; then\n'
        "    :\n"
        "  else\n"
        '    refuse 2 "cannot read checkout status; release state is unknown"\n'
        "  fi\n"
    )
    assert strict_read in source
    mutant = tmp_path / "release-mutant.sh"
    mutant.write_text(
        source.replace(
            strict_read,
            '  if dirty_state="$(git status --porcelain --untracked-files=normal)"; then\n'
            "    :\n"
            "  else\n"
            '    dirty_state=""\n'
            "  fi\n",
            1,
        ),
        encoding="utf-8",
    )
    mutant.chmod(0o755)
    run = _run_nested_release_test(
        tmp_path,
        "test_live_release_refuses_failed_checkout_status_read_before_publication",
        {"CSK_RELEASE_SCRIPT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_live_release_refuses_failed_checkout_status_read_before_publication" in run.stdout
    assert "1 failed" in run.stdout


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_discovers_runs_from_captured_real_provider_sample(
    tmp_path: Path,
) -> None:
    release_rows = json.loads((FIXTURES / "real-release-run-list.json").read_text())
    smoke_rows = json.loads(
        (FIXTURES / "real-distribution-smoke-run-list.json").read_text()
    )
    head = release_rows[0]["headSha"]
    result, calls, _ = _live_release(
        tmp_path,
        cut_head=head,
        release_runs_json=json.dumps(release_rows),
        smoke_runs_json=json.dumps(smoke_rows),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert release_rows[0]["headBranch"] != "v9.8.7"
    assert smoke_rows[0]["headBranch"] == "main"
    assert f"gh run watch {release_rows[0]['databaseId']}" in calls
    assert f"gh run watch {smoke_rows[0]['databaseId']}" in calls
    assert "--workflow release.yml" in calls
    assert "--workflow distribution-smoke.yml" in calls
    assert "--json databaseId,event,headBranch,headSha,createdAt,startedAt,url" in calls


def _foreign_smoke_run(head: str, *, created_at: str = "2026-09-29T10:02:00Z") -> str:
    return json.dumps(
        [
            {
                "databaseId": 202,
                "event": "workflow_run",
                "headBranch": "main",
                "headSha": head,
                "createdAt": created_at,
                "startedAt": "2026-09-29T10:02:30Z",
            }
        ]
    )


def _foreign_release_run(head: str) -> str:
    return json.dumps(
        [
            {
                "databaseId": 101,
                "event": "push",
                "headBranch": "v9.8.7",
                "headSha": head,
                "createdAt": "2026-09-29T10:00:00Z",
                "startedAt": "2026-09-29T10:01:00Z",
            }
        ]
    )


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_does_not_watch_a_foreign_release_sha(tmp_path: Path) -> None:
    result, calls, _ = _live_release(
        tmp_path,
        release_runs_json=_foreign_release_run("d" * 40),
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "release.yml" in result.stderr
    assert "gh run watch 101" not in calls
    assert "git push --no-follow-tags git@gitlab.wildberries.ru" not in calls
    assert "glab release create" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_release_run_failed_jobs_are_reported_as_red_before_wb_publication(
    tmp_path: Path,
) -> None:
    result, calls, _ = _live_release(
        tmp_path,
        release_watch_status=1,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "release.yml failed for v9.8.7" in result.stderr
    assert "release / ubuntu: https://github.com/ivanopcode/cocoaskills/actions/runs/101/job/2001" in result.stderr
    assert "git push --no-follow-tags git@gitlab.wildberries.ru" not in calls
    assert "glab release create" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_release_run_watch_failure_with_unreadable_jobs_is_unknown(
    tmp_path: Path,
) -> None:
    result, calls, _ = _live_release(
        tmp_path,
        release_watch_status=1,
        release_jobs_status=23,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "release.yml outcome could not be established" in result.stderr
    assert "Failed-job details for run 101 could not be read" in result.stderr
    assert "git push --no-follow-tags git@gitlab.wildberries.ru" not in calls
    assert "glab release create" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_admits_unknown_release_run_as_red_is_killed(
    tmp_path: Path,
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    strict_status = (
        '  if show_run_jobs "$run_id"; then\n'
        "    return 1\n"
        "  fi\n"
        "  return 2\n"
        "}\n\n"
        "watch_smoke_run() {\n"
    )
    assert strict_status in source
    mutant = tmp_path / "release-mutant.sh"
    mutant.write_text(
        source.replace(strict_status, strict_status.replace("  return 2\n", "  return 1\n", 1), 1),
        encoding="utf-8",
    )
    mutant.chmod(0o755)
    run = _run_nested_release_test(
        tmp_path,
        "test_release_run_watch_failure_with_unreadable_jobs_is_unknown",
        {"CSK_RELEASE_SCRIPT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_release_run_watch_failure_with_unreadable_jobs_is_unknown" in run.stdout
    assert "1 failed" in run.stdout


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_accepts_foreign_release_sha_is_killed(
    tmp_path: Path,
) -> None:
    source = SUPPORT_FILE.read_text(encoding="utf-8")
    exact_identity = (
        "        if row.get(\"event\") != expected_event or row.get(\"headSha\") != expected_head:\n"
        "            continue\n"
    )
    assert exact_identity in source
    mutant = tmp_path / "release-support-mutant.py"
    mutant.write_text(
        source.replace(
            exact_identity,
            "        if row.get(\"event\") != expected_event:\n"
            "            continue\n",
            1,
        ),
        encoding="utf-8",
    )
    run = _run_nested_release_test(
        tmp_path,
        "test_live_release_does_not_watch_a_foreign_release_sha",
        {"CSK_RELEASE_SUPPORT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_live_release_does_not_watch_a_foreign_release_sha" in run.stdout
    assert "1 failed" in run.stdout


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_does_not_watch_a_foreign_release_smoke_run(
    tmp_path: Path,
) -> None:
    result, calls, _ = _live_release(
        tmp_path,
        smoke_runs_json=_foreign_smoke_run("d" * 40),
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "distribution-smoke.yml run" in result.stderr
    assert "gh run watch 202" not in calls
    assert "PyPI" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_accepts_foreign_smoke_sha_is_killed(
    tmp_path: Path,
) -> None:
    source = SUPPORT_FILE.read_text(encoding="utf-8")
    exact_identity = (
        "        if row.get(\"event\") != expected_event or row.get(\"headSha\") != expected_head:\n"
        "            continue\n"
    )
    assert exact_identity in source
    mutant = tmp_path / "release-support-mutant.py"
    mutant.write_text(
        source.replace(
            exact_identity,
            "        if row.get(\"event\") != expected_event:\n"
            "            continue\n",
            1,
        ),
        encoding="utf-8",
    )
    run = _run_nested_release_test(
        tmp_path,
        "test_live_release_does_not_watch_a_foreign_release_smoke_run",
        {"CSK_RELEASE_SUPPORT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_live_release_does_not_watch_a_foreign_release_smoke_run" in run.stdout
    assert "1 failed" in run.stdout


@RUNS_POSIX_RELEASE_SCRIPT
def test_live_release_ignores_smoke_created_before_release_started(
    tmp_path: Path,
) -> None:
    stale_smoke = json.dumps(
        [
            {
                "databaseId": 202,
                "event": "workflow_run",
                "headBranch": "main",
                "headSha": "b" * 40,
                "createdAt": "2026-09-29T10:00:30Z",
                "startedAt": "2026-09-29T10:00:45Z",
            }
        ]
    )
    result, calls, _ = _live_release(tmp_path, smoke_runs_json=stale_smoke)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "distribution-smoke.yml run" in result.stderr
    assert "gh run watch 202" not in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_accepts_smoke_created_before_release_is_killed(
    tmp_path: Path,
) -> None:
    source = SUPPORT_FILE.read_text(encoding="utf-8")
    strict_time = (
        "        if threshold is not None and created_at <= threshold:\n"
        "            continue\n"
    )
    assert strict_time in source
    mutant = tmp_path / "release-support-mutant.py"
    mutant.write_text(
        source.replace(strict_time, "        if False:\n            continue\n", 1),
        encoding="utf-8",
    )
    run = _run_nested_release_test(
        tmp_path,
        "test_live_release_ignores_smoke_created_before_release_started",
        {"CSK_RELEASE_SUPPORT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_live_release_ignores_smoke_created_before_release_started" in run.stdout
    assert "1 failed" in run.stdout


@RUNS_POSIX_RELEASE_SCRIPT
def test_resume_from_verify_watches_manual_rerun_then_only_checks_distributions(
    tmp_path: Path,
) -> None:
    release_rows = json.loads((FIXTURES / "real-release-run-list.json").read_text())
    smoke_rows = json.loads(
        (FIXTURES / "real-distribution-smoke-run-list.json").read_text()
    )
    result, calls, _ = _live_release(
        tmp_path,
        cut_head=release_rows[0]["headSha"],
        release_runs_json=json.dumps(release_rows),
        smoke_runs_json=json.dumps(smoke_rows),
        resume=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Post-publish verification passed for v9.8.7" in result.stdout
    assert f"gh run watch {smoke_rows[0]['databaseId']}" in calls
    for forbidden in ("delivery trunk-verify", "git switch -c", "git commit -S", "git tag -s",
                      "git push", "gh pr create", "gh pr review", "glab release create",
                      "gh run rerun"):
        assert forbidden not in calls
    assert "curl -fsS" in calls
    assert "gh api" in calls
    assert "mise ls-remote pipx:cocoaskills" in calls


@RUNS_POSIX_RELEASE_SCRIPT
def test_smoke_failure_with_unreadable_jobs_is_unknown_but_never_reruns(
    tmp_path: Path,
) -> None:
    result, calls, _ = _live_release(
        tmp_path,
        smoke_watch_status=1,
        smoke_jobs_status=23,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "release itself has already been published" in result.stderr
    assert "Failed job links are unavailable" in result.stderr
    assert "gh run rerun 202 --failed" in result.stderr
    assert "gh run rerun" not in calls


def test_release_docs_describe_manual_smoke_recovery_and_verify_resume() -> None:
    docs = (ROOT / "RELEASING.md").read_text(encoding="utf-8")

    assert "gh run rerun <run-id> --failed" in docs
    assert "--resume-from verify" in docs
    assert "never reruns a smoke workflow automatically" in docs
    assert "release itself has already been published" in docs


@RUNS_POSIX_RELEASE_SCRIPT
def test_narrowing_mutant_that_reintroduces_gh_api_repo_flag_is_killed_by_live_test(
    tmp_path: Path,
) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    api_call = '  if formula_content="$(quiet_external gh api \\\n    "repos/${HOMEBREW_TAP_REPOSITORY}/contents/Formula/cocoaskills.rb" \\\n'
    assert api_call in source
    mutant = tmp_path / "release-mutant.sh"
    mutant.write_text(
        source.replace(
            api_call,
            '  if formula_content="$(quiet_external gh api --repo "$HOMEBREW_TAP_REPOSITORY" \\\n'
            '    "repos/${HOMEBREW_TAP_REPOSITORY}/contents/Formula/cocoaskills.rb" \\\n',
            1,
        ),
        encoding="utf-8",
    )
    mutant.chmod(0o755)
    run = _run_nested_release_test(
        tmp_path,
        "test_live_release_gates_the_exact_fast_forward_head_before_signing_tag",
        {"CSK_RELEASE_SCRIPT": str(mutant)},
    )

    assert run.returncode == 1, run.stdout + run.stderr
    assert "test_live_release_gates_the_exact_fast_forward_head_before_signing_tag" in run.stdout
    assert "1 failed" in run.stdout
