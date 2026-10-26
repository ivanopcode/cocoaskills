"""Project ignore contracts, evaluated by real Git in isolated repositories."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from conftest import make_project, run, write_skillfile
from csk import cli, config, gitignore_gate, manifest
from csk.sources.errors import SourceError
from test_mcp_project_install import PATHS, project_cli as gitignore_project_cli
from csk.sources import publish


@pytest.fixture
def project_cli(gitignore_project_cli):
    root, home, original_declare = gitignore_project_cli

    def declare(*args, **kwargs):
        original_declare(*args, **kwargs)
        data = json.loads((root / "Skillfile.json").read_text())
        data.pop("ignore_mode", None)
        write_skillfile(root, data)

    data = json.loads((root / "Skillfile.json").read_text())
    data.pop("ignore_mode", None)
    write_skillfile(root, data)
    return root, home, declare


# Invalid modes and schema-1 declarations have a stable declaration diagnostic.
@pytest.mark.parametrize("schema,value", [(1, "exclude"), (1, "gitignore"), (2, None),
                                           (2, True), (2, []), (3, 7), (3, "other")])
def test_invalid_mode(tmp_path, schema, value):
    root = make_project(tmp_path)
    write_skillfile(root, {"schema_version": schema, "ignore_mode": value, "skills": []})
    with pytest.raises(SourceError) as error:
        manifest.load_manifest(root)
    assert error.value.code == "ignore_mode_invalid"


# Both declared modes are accepted by both project grammars.
@pytest.mark.parametrize("schema", [2, 3])
@pytest.mark.parametrize("mode", ["exclude", "gitignore"])
def test_valid_mode(tmp_path, schema, mode):
    root = make_project(tmp_path)
    write_skillfile(root, {"schema_version": schema, "ignore_mode": mode, "skills": []})
    assert manifest.load_manifest(root).ignore_mode == mode


# Unsafe targets refuse before changing either the user file or the project policy.
@pytest.mark.parametrize("kind", ["symlink", "directory", "unwritable"])
def test_unsafe_exclude(tmp_path, kind):
    root = make_project(tmp_path)
    target = root / ".git/info/exclude"
    target.parent.mkdir(exist_ok=True)
    target.unlink(missing_ok=True)
    other = tmp_path / "user-data"
    other.write_bytes(b"user data\r\n")
    if kind == "symlink":
        target.symlink_to(other)
    elif kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"user data\r\n")
        target.chmod(0o444)
    before = (root / ".gitignore").read_bytes()
    try:
        with pytest.raises(gitignore_gate.GitignoreError) as error:
            gitignore_gate.ensure_ignored(root, [".agents/"], mode="exclude")
        assert error.value.code == "git_exclude_invalid"
        assert other.read_bytes() == b"user data\r\n"
        assert (root / ".gitignore").read_bytes() == before
    finally:
        if kind == "unwritable":
            target.chmod(0o644)


# Conflicting nested rules and tracked exact paths refuse without an exclude write.
@pytest.mark.parametrize("conflict", ["nested", "tracked"])
def test_exclude_verification_refuses(tmp_path, conflict):
    root = make_project(tmp_path, gitignore=False)
    (root / ".codex").mkdir()
    if conflict == "nested":
        (root / ".codex/.gitignore").write_text("!config.toml\n")
    else:
        (root / ".codex/config.toml").write_text("user = true\n")
        run(["git", "add", ".codex/config.toml"], root)
    target = root / ".git/info/exclude"
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(gitignore_gate.GitignoreError) as error:
        gitignore_gate.ensure_ignored(root, ["/.codex/config.toml"], mode="exclude")
    assert error.value.code == "gitignore_required"
    assert (target.read_bytes() if target.exists() else None) == before
    assert not (root / ".gitignore").exists()


# Init/install/status/uninstall preserve every .gitignore byte while real Git ignores owned paths.
@pytest.mark.parametrize("content", [None, b"", b"# user\r\nuser-file\r\n"])
def test_exclude_lifecycle(project_cli, content, capsys):
    root, _, declare = project_cli
    ignore = root / ".gitignore"
    if content is None:
        ignore.unlink()
    else:
        ignore.write_bytes(content)
    assert cli.main(["init", str(root), "--no-interactive"]) == 0
    target = root / ".git/info/exclude"
    user = b"# local user\r\n/local-only\r\n"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(user)
    for command in (["install", "app", "--fix-gitignore"], ["install", "app"], ["status", "app"]):
        assert cli.main(command) == 0
        assert (ignore.read_bytes() if ignore.exists() else None) == content
    output = capsys.readouterr()
    assert "no-op in exclude mode" in output.out
    assert target.read_bytes().startswith(user)
    assert target.read_bytes().count(b"# CocoaSkill project ") == 1
    assert target.read_bytes().count(b"# End CocoaSkill project ") == 1
    for path in [".agents/probe", "Skillfile.dev.json", *PATHS]:
        assert run(["git", "check-ignore", "-q", "--", path], root, check=False).returncode == 0
    declare({})
    assert cli.main(["install", "app"]) == 0
    assert all(not (root / path).exists() for path in PATHS)
    assert (ignore.read_bytes() if ignore.exists() else None) == content


# Worktrees share common info/exclude but own separate idempotent project blocks.
def test_common_exclude(tmp_path):
    root = make_project(tmp_path, gitignore=False)
    run(["git", "commit", "--allow-empty", "-m", "fixture"], root)
    linked = tmp_path / "linked"
    run(["git", "worktree", "add", "-b", "linked", str(linked)], root)
    target = root / ".git/info/exclude"
    user = b"# user\r\n*.private\r\n"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(user)
    gitignore_gate.ensure_ignored(linked, [".agents/"], mode="exclude")
    gitignore_gate.ensure_ignored(root, [".agents/"], mode="exclude")
    assert target.read_bytes().startswith(user)
    assert target.read_bytes().count(b"# CocoaSkill project ") == 2
    assert (linked / ".git").is_file()
    assert not (linked / ".gitignore").exists()
    assert run(["git", "check-ignore", "-q", ".agents/probe"], linked).returncode == 0


# An existing unconfigured managed block retains legacy writes and gains only a status advisory.
def test_legacy_advisory(project_cli, capsys):
    root, _, _ = project_cli
    ignore = root / ".gitignore"
    ignore.write_text("# CocoaSkill\n.agents/\n.claude/skills/\n.codex/skills/\n.cursor/rules/\n")
    assert cli.main(["install", "app"]) == 0
    before = ignore.read_bytes()
    assert cli.main(["status", "app"]) == 0
    assert '"ignore_mode": "gitignore"' in capsys.readouterr().out
    assert ignore.read_bytes() == before


# Explicit exclude overrides a legacy block without editing it; dry-run writes no exclude/configs.
def test_explicit_exclude_dry_run(project_cli):
    root, _, _ = project_cli
    path = root / "Skillfile.json"
    data = json.loads(path.read_text())
    data["ignore_mode"] = "exclude"
    write_skillfile(root, data)
    before = (root / ".gitignore").read_bytes()
    target = root / ".git/info/exclude"
    old = target.read_bytes() if target.exists() else None
    assert cli.main(["install", "app", "--dry-run"]) == 0
    assert (target.read_bytes() if target.exists() else None) == old
    assert all(not (root / path).exists() for path in PATHS)
    assert cli.main(["install", "app"]) == 0
    assert b"/.mcp.json\n" in target.read_bytes()
    assert (root / ".gitignore").read_bytes() == before


# Publication faults restore exclude, native configs and lock, including schema 2 without MCP.
@pytest.mark.parametrize("schema", [2, 3])
@pytest.mark.parametrize("before", [None, b"# user\r\n*.private\r\n", b"# user without final newline"])
def test_exclude_transaction_rollback(project_cli, monkeypatch, schema, before):
    root, _, declare = project_cli
    if schema == 2:
        declare(None, schema=2)
    (root / ".gitignore").unlink()
    target = root / ".git/info/exclude"
    target.parent.mkdir(exist_ok=True)
    if before is not None:
        target.write_bytes(before)
    original = publish.TransactionEngine
    reached = []

    def factory(home, **kwargs):
        def fault(point, item):
            target_class = "mcp-gitignore" if schema == 2 else "mcp-native"
            if point == "target_committed" and item is not None and item.target_class == target_class and not reached:
                reached.append(point)
                raise OSError("injected native publication failure")
        return original(home, fault_hook=fault, **kwargs)

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    assert cli.main(["install", "app"]) == 1
    assert reached == ["target_committed"]
    assert (target.read_bytes() if target.exists() else None) == before
    assert all(not (root / path).exists() for path in PATHS)
    assert not (root / "Skillfile.lock.json").exists()
    assert not (root / ".gitignore").exists()


# A common exclude block is replaced in place without altering non-UTF-8/CRLF user bytes.
def test_block_in_place(tmp_path):
    root = make_project(tmp_path, gitignore=False)
    target = root / ".git/info/exclude"
    target.parent.mkdir(exist_ok=True)
    before, after = b"# user \xff\r\n", b"# tail\r\n/private\r\n"
    key = gitignore_gate.project_key(root)
    target.write_bytes(before + f"# CocoaSkill project {key}\r\n/old-owned/\r\n# End CocoaSkill project {key}\r\n".encode() + after)
    gitignore_gate.ensure_ignored(root, [".agents/"], mode="exclude")
    assert target.read_bytes().startswith(before)
    assert target.read_bytes().endswith(after)
    assert b"/old-owned/" not in target.read_bytes()
    once = target.read_bytes()
    gitignore_gate.ensure_ignored(root, [".agents/"], mode="exclude")
    assert target.read_bytes() == once


# A later user negation in info/exclude still defeats the managed block and refuses without writes.
def test_exclude_user_negation(tmp_path):
    root = make_project(tmp_path, gitignore=False)
    target = root / ".git/info/exclude"
    target.parent.mkdir(exist_ok=True)
    key = gitignore_gate.project_key(root)
    original = f"# CocoaSkill project {key}\n/.mcp.json\n# End CocoaSkill project {key}\n!/.mcp.json\n".encode()
    target.write_bytes(original)
    with pytest.raises(gitignore_gate.GitignoreError, match="does not effectively ignore"):
        gitignore_gate.ensure_ignored(root, ["/.mcp.json"], mode="exclude")
    assert target.read_bytes() == original


# A nested project anchors its owned file at its worktree-relative path, not a sibling's file.
def test_nested_project_exclude(tmp_path):
    root = make_project(tmp_path, gitignore=False)
    nested = root / "nested[1]"
    nested.mkdir()
    gitignore_gate.ensure_ignored(nested, ["/.mcp.json"], mode="exclude")
    assert run(["git", "check-ignore", "-q", "nested[1]/.mcp.json"], root).returncode == 0
    assert run(["git", "check-ignore", "-q", ".mcp.json"], root, check=False).returncode == 1


# Native installation in a linked worktree publishes its ignore transaction to the shared common dir.
def test_linked_worktree_install(project_cli, tmp_path):
    root, home, _ = project_cli
    linked = tmp_path / "linked"
    run(["git", "worktree", "add", "-b", "linked", str(linked)], root)
    (linked / "Skillfile.json").write_bytes((root / "Skillfile.json").read_bytes())
    cfg = config.load_config(home / "config.json")
    cfg.projects["app"] = replace(cfg.projects["app"], path=linked)
    config.save_config(cfg)
    before = (linked / ".gitignore").read_bytes()
    assert cli.main(["install", "app"]) == 0
    target = root / ".git/info/exclude"
    assert b"/.mcp.json\n" in target.read_bytes()
    assert (linked / ".git").is_file()
    assert (linked / ".gitignore").read_bytes() == before
    assert (linked / ".mcp.json").exists()
    assert not (root / ".mcp.json").exists()


# Unsafe exclude and tracked manager-created MCP outputs refuse with precise codes and no partial configs.
@pytest.mark.parametrize("kind,code", [("directory", "git_exclude_invalid"), ("tracked", "gitignore_required")])
def test_install_ignore_refusal(project_cli, capsys, kind, code):
    root, _, _ = project_cli
    (root / ".gitignore").unlink()
    target = root / ".git/info/exclude"
    if kind == "directory":
        target.mkdir(parents=True)
    else:
        assert cli.main(["install", "app"]) == 0
        run(["git", "add", "-f", ".mcp.json"], root)
    original = (root / ".mcp.json").read_bytes() if (root / ".mcp.json").exists() else None
    assert cli.main(["install", "app"]) == 1
    assert code in capsys.readouterr().err
    assert ((root / ".mcp.json").read_bytes() if (root / ".mcp.json").exists() else None) == original
    assert not (root / ".gitignore").exists()


# Conditional live gitdir settings retain case-insensitive negations in staged exclude verification.
@pytest.mark.parametrize("ignore_case", [True, False])
def test_conditional_ignore_case(tmp_path, ignore_case):
    root = make_project(tmp_path, gitignore=False)
    policy = tmp_path / "case-policy"
    policy.write_text(f"[core]\nignoreCase = {str(ignore_case).lower()}\n")
    run(["git", "config", "includeIf.gitdir:" + (root / ".git").resolve().as_posix() + ".path", str(policy)], root)
    assert run(["git", "config", "--bool", "--get", "core.ignoreCase"], root).stdout.strip() == str(ignore_case).lower()
    (root / ".gitignore").write_text("!/.MCP.JSON\n")
    if ignore_case:
        with pytest.raises(gitignore_gate.GitignoreError, match="does not effectively ignore"):
            gitignore_gate.ensure_ignored(root, ["/.mcp.json"], mode="exclude")
        assert not (root / ".git/info/exclude").exists()
    else:
        gitignore_gate.ensure_ignored(root, ["/.mcp.json"], mode="exclude")
        assert run(["git", "check-ignore", "-q", "--", ".mcp.json"], root).returncode == 0


# Init rejects unsafe exclude before creating a Skillfile; a regular target permits initialization.
@pytest.mark.parametrize("unsafe", [True, False])
def test_init_exclude_refusal(tmp_path, capsys, unsafe):
    root = make_project(tmp_path, gitignore=False)
    target = root / ".git/info/exclude"
    target.parent.mkdir(exist_ok=True)
    if unsafe:
        target.mkdir()
    else:
        target.write_bytes(b"# user\r\n")
    assert cli.main(["init", str(root), "--no-interactive"]) == (cli.EXIT_CONFIG if unsafe else 0)
    if unsafe:
        assert "git_exclude_invalid" in capsys.readouterr().err
        assert not (root / "Skillfile.json").exists()
        assert target.is_dir()
    else:
        assert (root / "Skillfile.json").is_file()
        assert target.read_bytes().startswith(b"# user\r\n")
    assert not (root / ".gitignore").exists()
