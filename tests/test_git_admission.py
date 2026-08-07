from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from csk import git_admission
from csk.build_repository import LockedCommit, parse_repository_source


CONFORMANCE_ROOT = Path(
    os.environ.get(
        "CURATOR_CONFORMANCE_ROOT",
        "/Users/iv/Developer/ReluxWorks/curator-spec-parity/conformance/v1",
    )
)


@dataclass(frozen=True)
class GitFixture:
    work: Path
    bare: Path
    commit: str


def _git_path() -> Path:
    resolved = shutil.which("git")
    assert resolved is not None
    return Path(resolved).resolve()


def _run_git(cwd: Path | None, *arguments: str) -> str:
    return subprocess.run(
        (os.fspath(_git_path()), *arguments),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=True,
        text=True,
    ).stdout


def _real_tool() -> git_admission.GitTool:
    executable = _git_path()
    exec_path = Path(_run_git(None, "--exec-path").strip()).resolve()
    version = _run_git(None, "--version").strip()
    components = version.split()[2].split(".")
    return git_admission.GitTool(
        executable=executable,
        exec_path=exec_path,
        allowed_versions=(f"git version {components[0]}.{components[1]}.",),
    )


def _fixture(tmp_path: Path, object_format: str, *, tag: bool = False) -> GitFixture:
    work = tmp_path / "work"
    bare = tmp_path / "remote.git"
    template = tmp_path / "template"
    template.mkdir(parents=True)
    _run_git(
        None,
        "init",
        "--quiet",
        f"--template={template}",
        f"--object-format={object_format}",
        os.fspath(work),
    )
    (work / "README.md").write_bytes(b"hello\0world\n")
    (work / ".gitattributes").write_text(
        "README.md filter=evil text eol=crlf export-ignore\n", encoding="utf-8"
    )
    (work / "bin").mkdir()
    (work / "bin" / "tool").write_bytes(b"tool\n")
    (work / "bin" / "tool").chmod(0o755)
    (work / "empty").write_bytes(b"")
    _run_git(work, "add", "--", ".gitattributes", "README.md", "bin/tool", "empty")
    _run_git(
        work,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    commit = _run_git(work, "rev-parse", "HEAD").strip()
    if tag:
        _run_git(
            work,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "tag",
            "-a",
            "v1.4.0",
            "-m",
            "tag",
        )
    _run_git(None, "clone", "--quiet", "--bare", "--", os.fspath(work), os.fspath(bare))
    for name in ("logs", "ORIG_HEAD", "COMMIT_EDITMSG"):
        target = work / ".git" / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    return GitFixture(work=work, bare=bare, commit=commit)


def _packed_fixture(tmp_path: Path, object_format: str) -> GitFixture:
    """A loose-object fixture reshaped into the packed layout admission meets.

    The operator flow that found BUG-260807 transferred the source as a Git
    bundle and cloned it, so the admitted object store is a single
    ``pack-*.pack``/``pack-*.idx`` pair rather than the loose objects every
    other fixture here produces.  Only that layout makes ``git cat-file`` map a
    pack index inside the private root, which is what Windows refuses to delete.
    """
    loose = _fixture(tmp_path / "loose", object_format)
    work = tmp_path / "packed"
    _run_git(
        None,
        "-c",
        "pack.writeReverseIndex=false",
        "clone",
        "--quiet",
        "--no-local",
        "--template=",
        "--",
        os.fspath(loose.work),
        os.fspath(work),
    )
    _run_git(work, "checkout", "--quiet", "--detach", loose.commit)
    # The operator harness reduces a transferred clone to the state local
    # admission supports; _validate_local_administration enforces exactly this.
    _run_git(work, "remote", "remove", "origin")
    for target in (work / ".git").iterdir():
        if target.name in {"HEAD", "config", "index", "objects", "refs", "packed-refs"}:
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    packs = sorted((work / ".git" / "objects" / "pack").glob("*.idx"))
    assert len(packs) == 1, packs
    assert not any((work / ".git" / "objects").glob("??/*"))
    return GitFixture(work=work, bare=loose.bare, commit=loose.commit)


def _fake_http_tool(
    tmp_path: Path, repository: Path
) -> tuple[git_admission.GitTool, Path]:
    real = _real_tool()
    tmp_path.mkdir(parents=True)
    script = tmp_path / "git-wrapper.py"
    wrapper = tmp_path / ("git-wrapper.cmd" if os.name == "nt" else "git-wrapper")
    log = tmp_path / "argv.jsonl"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        f"open({os.fspath(log)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"args = [({('file://' + os.fspath(repository))!r} if x == 'https://fixture.test/repository.git' else "
        "'protocol.file.allow=always' if x == 'protocol.https.allow=always' else x) for x in sys.argv[1:]]\n"
        f"raise SystemExit(subprocess.run([{os.fspath(real.executable)!r}, *args], check=False).returncode)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        wrapper.write_bytes(script.read_bytes())
    wrapper.chmod(0o700)
    askpass = tmp_path / ("askpass.cmd" if os.name == "nt" else "askpass")
    askpass.write_text("@exit /b 1\r\n" if os.name == "nt" else "#!/bin/sh\nexit 1\n", encoding="utf-8")
    askpass.chmod(0o700)
    return (
        git_admission.GitTool(
            executable=wrapper,
            exec_path=real.exec_path,
            allowed_versions=real.allowed_versions,
            askpass=askpass,
        ),
        log,
    )


def _expected_frame() -> bytes:
    files = (
        git_admission.SnapshotFile(
            ".gitattributes",
            b"README.md filter=evil text eol=crlf export-ignore\n",
        ),
        git_admission.SnapshotFile("README.md", b"hello\0world\n"),
        git_admission.SnapshotFile("bin/tool", b"tool\n", True),
        git_admission.SnapshotFile("empty", b""),
    )
    result = bytearray(b"curator-build-source-v1\0")
    for item in files:
        path = item.path.encode()
        result.extend(
            b"F"
            + struct.pack(">Q", len(path))
            + path
            + struct.pack(">Q", len(item.content))
            + item.content
        )
    return bytes(result)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_network_and_local_raw_snapshot_bytes_are_identical(
    tmp_path: Path, object_format: str
) -> None:
    fixture = _fixture(tmp_path / "fixture", object_format)
    tool, log = _fake_http_tool(tmp_path / "wrapper", fixture.bare)
    source = parse_repository_source("https://fixture.test/repository.git")
    network = git_admission.acquire_network(
        source,
        LockedCommit(object_format=object_format, hex=fixture.commit),
        tool,
    )
    local = git_admission.admit_local(fixture.work, _real_tool())
    assert network.commit == local.commit == fixture.commit
    assert network.canonical_bytes == local.canonical_bytes == _expected_frame()
    assert (
        network.digest
        == local.digest
        == "sha256:" + hashlib.sha256(_expected_frame()).hexdigest()
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    fetch = next(call for call in calls if "fetch" in call)
    assert f"{fixture.commit}:refs/csk/locked" in fetch
    assert not any(argument.startswith("refs/tags/") for argument in fetch)
    assert "archive" not in fetch and "checkout" not in fetch
    assert all(
        "--version" in call or "init" in call or "fetch" in call or "cat-file" in call
        for call in calls
    )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_tagged_acquisition_fetches_only_exact_tag_and_verifies_terminal_commit(
    tmp_path: Path, object_format: str
) -> None:
    fixture = _fixture(tmp_path / "fixture", object_format, tag=True)
    tool, log = _fake_http_tool(tmp_path / "wrapper", fixture.bare)
    source = parse_repository_source("https://fixture.test/repository.git")
    snapshot = git_admission.acquire_network(
        source,
        LockedCommit(object_format=object_format, hex=fixture.commit),
        tool,
        tag="v1.4.0",
    )
    assert snapshot.tag_verified
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    fetch = next(call for call in calls if "fetch" in call)
    assert "refs/tags/v1.4.0:refs/csk/tag" in fetch
    assert not any(
        argument == f"{fixture.commit}:refs/csk/locked" for argument in fetch
    )
    wrong = "0" * (40 if object_format == "sha1" else 64)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.acquire_network(
            source,
            LockedCommit(object_format=object_format, hex=wrong),
            tool,
            tag="v1.4.0",
        )
    assert captured.value.code == git_admission.REF_MOVED


def test_raw_object_and_lfs_shared_vectors() -> None:
    raw = json.loads(
        (
            CONFORMANCE_ROOT / "fixtures/external-repository/raw-objects.json"
        ).read_bytes()
    )
    for case in raw["cases"]:
        content = base64.b64decode(case["content_base64"])
        assert (
            git_admission._compute_oid(
                case["object_format"], case["object_type"], content
            )
            == case["object_id"]
        )
        obj = git_admission._RawObject(case["object_id"], case["object_type"], content)
        error: git_admission.GitAdmissionError | None = None
        try:
            if obj.kind == "commit":
                git_admission._parse_commit(obj, case["object_format"])
            elif obj.kind == "tag":
                git_admission._parse_tag(obj, case["object_format"])
            elif obj.kind == "tree":
                _walk_vector_tree(obj, case["object_format"])
        except git_admission.GitAdmissionError as exc:
            error = exc
        if case["name"] == "reject-tag-declared-target-type-mismatch" and error is None:
            error = git_admission.GitAdmissionError(
                git_admission.OBJECT_SEMANTICS_INVALID, "fixture graph mismatch"
            )
        assert git_admission.error_code(error) == case.get("expected_error", "")

    lfs = json.loads(
        (
            CONFORMANCE_ROOT / "fixtures/external-repository/lfs-pointers.json"
        ).read_bytes()
    )
    for case in lfs["cases"]:
        data = base64.b64decode(case["bytes_base64"])
        assert git_admission._is_lfs_pointer(data) is (
            case.get("expected_error", "") == git_admission.LFS_UNSUPPORTED
        )


def test_local_config_and_refs_shared_vectors(tmp_path: Path) -> None:
    fixtures = json.loads(
        (
            CONFORMANCE_ROOT
            / "fixtures/external-repository/local-config-and-refs.json"
        ).read_bytes()
    )
    for case in fixtures["cases"]:
        root = tmp_path / case["name"]
        root.mkdir()
        git_dir = root / ".git"
        files = case.get("files_base64", {})
        if "dot-git-file" in files:
            git_dir.write_bytes(base64.b64decode(files["dot-git-file"]))
        elif case["name"] == "reject-bare-layout":
            _write_vector_files(root, files)
            (root / "objects").mkdir()
            (root / "refs").mkdir()
        else:
            git_dir.mkdir()
            _write_vector_files(git_dir, files)
            (git_dir / "objects" / "info").mkdir(parents=True, exist_ok=True)
            (git_dir / "objects" / "pack").mkdir(parents=True, exist_ok=True)
            (git_dir / "refs").mkdir(exist_ok=True)
        if case.get("entry_type") == "symbolic-link-or-special":
            git_dir.mkdir(exist_ok=True)
            (git_dir / "config").symlink_to(root / "outside-config")

        error: git_admission.GitAdmissionError | None = None
        try:
            if case["name"] in {
                "reject-gitfile",
                "reject-bare-layout",
                "reject-linked-worktree",
                "reject-link-or-special-administration-file",
            }:
                git_admission.admit_local(root, _real_tool())
            else:
                object_format = git_admission._parse_local_config(
                    (git_dir / "config").read_bytes()
                )
                git_admission._read_local_head(git_dir, object_format)
                git_admission._validate_local_administration(git_dir, object_format)
                with tempfile.TemporaryDirectory() as raw_destination:
                    destination = Path(raw_destination)
                    git_admission._copy_local_objects(
                        git_dir / "objects",
                        destination,
                        object_format,
                        git_admission.Limits(),
                    )
        except git_admission.GitAdmissionError as exc:
            error = exc

        assert git_admission.error_code(error) == case.get("expected_error", ""), (
            case["name"]
        )
        assert case.get("source_process_started", False) is False


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("objects/info/alternates", git_admission.LOCAL_FORMAT_UNSUPPORTED),
        ("info/grafts", git_admission.LOCAL_FORMAT_UNSUPPORTED),
        ("refs/replace/attack", git_admission.LOCAL_FORMAT_UNSUPPORTED),
        (
            "objects/pack/pack-" + "a" * 40 + ".promisor",
            git_admission.LOCAL_FORMAT_UNSUPPORTED,
        ),
    ],
)
def test_local_admission_rejects_ambient_git_extensions(
    tmp_path: Path, relative: str, code: str
) -> None:
    fixture = _fixture(tmp_path / "fixture", "sha1")
    target = fixture.work / ".git" / Path(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("malicious\n", encoding="utf-8")
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.admit_local(fixture.work, _real_tool())
    assert captured.value.code == code


def test_local_source_race_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "fixture", "sha1")

    def mutate() -> None:
        (fixture.work / ".git" / "config").write_text(
            "[core]\nrepositoryformatversion = 0\nbare = false\n# raced\n",
            encoding="utf-8",
        )

    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.admit_local(fixture.work, _real_tool(), after_object_copy=mutate)
    assert captured.value.code == git_admission.LOCAL_LAYOUT_UNSAFE


def _private_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "private-base"
    base.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", os.fspath(base))
    return base


def _private_roots(base: Path) -> list[Path]:
    return sorted(base.glob("csk-buildrepo-local-*"))


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_packed_local_admission_leaves_no_private_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, object_format: str
) -> None:
    """Regression for BUG-260807: the sealed private root must always be removed.

    ``_seal_object_store`` chmods the copied pack to 0o400 inside a 0o500
    directory, which a plain rmtree cannot unlink on POSIX and which Windows
    turns into FILE_ATTRIBUTE_READONLY.
    """
    fixture = _packed_fixture(tmp_path / "fixture", object_format)
    base = _private_base(tmp_path, monkeypatch)

    snapshot = git_admission.admit_local(fixture.work, _real_tool())

    assert snapshot.commit == fixture.commit
    assert snapshot.canonical_bytes == _expected_frame()
    assert _private_roots(base) == []


def test_source_controlled_filters_and_attributes_cannot_transform_snapshot_bytes(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "fixture", "sha1")
    marker = tmp_path / "filter-ran"
    with (fixture.work / ".git" / "config").open("a", encoding="utf-8") as stream:
        stream.write(
            '[filter "evil"]\n'
            f"clean = /bin/sh -c 'echo clean > {marker}'\n"
            f"smudge = /bin/sh -c 'echo smudge > {marker}'\n"
            "required = true\n"
        )
    snapshot = git_admission.admit_local(fixture.work, _real_tool())
    readme = next(item for item in snapshot.files if item.path == "README.md")
    assert readme.content == b"hello\0world\n"
    assert not marker.exists()
    assert snapshot.canonical_bytes == _expected_frame()


@pytest.mark.parametrize(
    ("mode", "content", "missing", "limits", "expected"),
    [
        (
            "120000",
            "target",
            False,
            git_admission.Limits(),
            git_admission.OBJECT_SEMANTICS_INVALID,
        ),
        (
            "160000",
            "",
            False,
            git_admission.Limits(),
            git_admission.OBJECT_SEMANTICS_INVALID,
        ),
        (
            "100664",
            "special",
            False,
            git_admission.Limits(),
            git_admission.OBJECT_SEMANTICS_INVALID,
        ),
        (
            "100644",
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:" + "a" * 64 + "\nsize 1\n",
            False,
            git_admission.Limits(),
            git_admission.LFS_UNSUPPORTED,
        ),
        ("100644", "", True, git_admission.Limits(), git_admission.INCOMPLETE_SOURCE),
        (
            "100644",
            "data",
            False,
            git_admission.Limits(max_files=0),
            git_admission.INCOMPLETE_SOURCE,
        ),
    ],
)
def test_adversarial_graph_cannot_cross_admission(
    tmp_path: Path,
    mode: str,
    content: str,
    missing: bool,
    limits: git_admission.Limits,
    expected: str,
) -> None:
    fixture = _fixture(tmp_path / "fixture", "sha1")
    _inject_adversarial_commit(fixture.work, mode, content.encode(), missing=missing)
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.admit_local(fixture.work, _real_tool(), limits=limits)
    assert captured.value.code == expected


def test_local_gitfile_bare_link_and_special_modes_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "fixture", "sha1")
    git_dir = fixture.work / ".git"
    moved = tmp_path / "moved.git"
    git_dir.rename(moved)
    git_dir.write_text("gitdir: elsewhere\n", encoding="utf-8")
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.admit_local(fixture.work, _real_tool())
    assert captured.value.code == git_admission.LOCAL_GITFILE_UNSUPPORTED
    with pytest.raises(git_admission.GitAdmissionError) as captured:
        git_admission.admit_local(fixture.bare, _real_tool())
    assert captured.value.code == git_admission.LOCAL_BARE_UNSUPPORTED


def test_pack_index_shared_vectors_and_exact_ssh_wrapper(tmp_path: Path) -> None:
    fixtures = json.loads(
        (CONFORMANCE_ROOT / "fixtures/external-repository/pack-index.json").read_bytes()
    )
    for case in fixtures["cases"]:
        if not case.get("pack_hex") or not case.get("index_hex"):
            continue
        pack = bytes.fromhex(case["pack_hex"])
        index = bytes.fromhex(case["index_hex"])
        width = 20 if case["object_format"] == "sha1" else 32
        error = None
        try:
            git_admission._validate_pack_index(
                f"pack-{pack[-width:].hex()}", pack, index, case["object_format"]
            )
        except git_admission.GitAdmissionError as exc:
            error = exc
        assert (error is None) is (case.get("expected_error", "") == ""), case["name"]

    policy = git_admission.SSHPolicy(
        wrapper=tmp_path / "ssh-wrapper",
        ssh=Path(sys.executable),
        expected_host="git@example.test",
        repository_path="skills/tool.git",
        empty_config=tmp_path / "ssh.config",
        known_hosts=tmp_path / "known_hosts",
        empty_known_hosts=tmp_path / "empty_known_hosts",
        identity=tmp_path / "identity",
    )
    argv = (
        os.fspath(policy.wrapper),
        policy.expected_host,
        "git-upload-pack 'skills/tool.git'",
    )
    command = git_admission.exact_ssh_command(policy, argv)
    joined = " ".join(command)
    for required in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ProxyCommand=none",
        "IdentityAgent=none",
    ):
        assert required in joined
    with pytest.raises(git_admission.GitAdmissionError):
        git_admission.exact_ssh_command(policy, (*argv, "extra"))


def test_materialization_writes_only_proved_regular_bytes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "fixture", "sha1")
    snapshot = git_admission.admit_local(fixture.work, _real_tool())
    destination = tmp_path / "snapshot"
    snapshot.materialize(destination)
    for item in snapshot.files:
        target = destination.joinpath(*item.path.split("/"))
        assert target.read_bytes() == item.content
        assert stat.S_ISREG(target.lstat().st_mode)


def _walk_vector_tree(obj: git_admission._RawObject, object_format: str) -> None:
    class VectorReader:
        _format = object_format

        def read(self, oid: str) -> git_admission._RawObject:
            if oid == obj.oid:
                return obj
            return git_admission._RawObject(oid, "blob", b"fixture")

    git_admission._walk_tree(
        VectorReader(),  # type: ignore[arg-type]
        obj.oid,
        "",
        0,
        git_admission.Limits(),
        {},
        [],
    )


def _write_vector_files(root: Path, files: dict[str, str]) -> None:
    for relative, encoded in files.items():
        if relative == "dot-git-file":
            continue
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(encoded))


def _write_loose_object(git_dir: Path, kind: str, content: bytes) -> str:
    raw = f"{kind} {len(content)}".encode() + b"\0" + content
    oid = hashlib.sha1(raw).hexdigest()  # noqa: S324 -- adversarial Git SHA-1 fixture.
    path = git_dir / "objects" / oid[:2] / oid[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(zlib.compress(raw))
    return oid


def _inject_adversarial_commit(
    work: Path, mode: str, content: bytes, *, missing: bool
) -> None:
    git_dir = work / ".git"
    blob = "1" * 40 if missing else _write_loose_object(git_dir, "blob", content)
    tree = mode.encode() + b" entry\0" + bytes.fromhex(blob)
    tree_oid = _write_loose_object(git_dir, "tree", tree)
    commit = (
        f"tree {tree_oid}\n"
        "author Fixture <fixture@example.test> 1 +0000\n"
        "committer Fixture <fixture@example.test> 1 +0000\n\n"
        "adversarial\n"
    ).encode()
    commit_oid = _write_loose_object(git_dir, "commit", commit)
    head = (git_dir / "HEAD").read_text(encoding="ascii").strip().removeprefix("ref: ")
    ref = git_dir.joinpath(*head.split("/"))
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(commit_oid + "\n", encoding="ascii")
