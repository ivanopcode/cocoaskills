"""Bounded header admission and modeled Windows PATH choices; no helper dispatch."""
import os
import ntpath
import stat
import shutil
import struct
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from mcp_native_fixture import native_header, fat_header

from csk import cli, mcp_command_resolution as resolution, mcp_declarations as mcp, mcp_project
from test_mcp_command_resolution import executable, parse, stdio
from test_mcp_install_acceptance import _snapshot
from test_mcp_project_install import project_cli

pytestmark = pytest.mark.usefixtures("stable_env")


def local_os(monkeypatch, **changes):
    # Replace this module's binding, never shared os functions used by pytest.
    monkeypatch.setattr(resolution, "os", SimpleNamespace(**{**vars(os), **changes}))


def terminal_views(monkeypatch):
    class Terminal:
        def __init__(self, stream):
            self.stream = stream
        def isatty(self):
            return True
        def __getattr__(self, name):
            return getattr(self.stream, name)
    view = SimpleNamespace(**{**vars(sys), "stdin": Terminal(sys.stdin), "stdout": Terminal(sys.stdout)})
    monkeypatch.setattr(cli, "sys", view)
    monkeypatch.setattr(mcp_project, "sys", view)


def host(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(resolution, "sys", SimpleNamespace(platform=platform), raising=False)
    monkeypatch.setattr(resolution, "_is_windows", lambda: platform == "win32")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(resolution, "_BASE_DIRECTORIES", ())
    monkeypatch.setattr(resolution, "_SYSTEM_DIRECTORIES", ())
    root = tmp_path / "Windows"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("SystemRoot", str(root))


# Foreign formats and decidably incomplete fixed headers refuse, while nearby
# opaque native headers pass. These fixtures do not qualify a kernel loader.
@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("format", ["darwin", "linux", "win32"])
def test_current_platform_only(tmp_path, monkeypatch, platform, format):
    host(monkeypatch, tmp_path, platform)
    executable(tmp_path / ("my-server.exe" if platform == "win32" else "my-server"), native_header(format))
    if platform == format:
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    else:
        with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
            resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)


@pytest.mark.parametrize("header", [b"", b"text", bytes.fromhex("CFFAEDFE"), bytes.fromhex("CFFAEDFE") + b"\0" * 40,
    bytes.fromhex("BEBAFECA01000000"), bytes.fromhex("CAFEBABE00000001"), native_header("darwin")[:12]])
def test_malformed_mac_header(tmp_path, monkeypatch, header):
    host(monkeypatch, tmp_path, "darwin")
    executable(tmp_path / "my-server", header)
    with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)


# Opaque interpreter observation stays exactly two bytes: reject short, text,
# foreign and nested prefixes; admit only a current-platform native prefix.
@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("kind", ["native", "foreign", "empty", "short", "text", "nested"])
def test_opaque_interpreter_prefix(tmp_path, monkeypatch, platform, kind):
    host(monkeypatch, tmp_path, platform)
    bodies = {"native": native_header(platform), "foreign": native_header("linux" if platform == "darwin" else "darwin"),
              "empty": b"", "short": b"\xcf", "text": b"text", "nested": b"#!/unknown\n"}
    interpreter = executable(tmp_path / "opaque", bodies[kind])
    executable(tmp_path / "my-server", b"#!" + os.fsencode(interpreter) + b"\n")
    reads = []
    def read(fd, size):
        reads.append(size)
        return os.read(fd, size)
    local_os(monkeypatch, read=read)
    if kind == "native":
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    else:
        with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
            resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    assert reads == [1024, 2]


def windows_files(tmp_path, monkeypatch, kind):
    host(monkeypatch, tmp_path, "win32")
    first = tmp_path / "first"; first.mkdir()
    later = tmp_path / "later"; later.mkdir()
    evaluator = executable(first / "node.exe", b"MZfixture")
    command = "my-server"
    if kind in {"same", "later"}:
        executable(first / "my-server.com", b"MZopaque")
        os.link(evaluator, (first if kind == "same" else later) / "my-server.exe")
    elif kind == "quoted":
        os.link(evaluator, first / "my-server.exe")
        executable(later / "my-server.exe", b"MZopaque")
    elif kind == "root":
        first = tmp_path / "Windows"
        executable(first / "my-server.exe", b"MZopaque")
        monkeypatch.setenv("SystemRoot", '"' + str(first) + '"')
    else:
        command = "my-server.custom"
        os.link(evaluator, first / (command + ".exe"))
    monkeypatch.setenv("PATH", ('"' + str(first) + '"' if kind in {"quoted", "root"} else str(first)) + os.pathsep + str(later))
    return command


# Each previously invisible dispatch choice is an evaluator inode or SystemRoot
# file. The independent expected result is refusal, not equivalence to a model.
@pytest.mark.parametrize("kind", ["same", "later", "quoted", "root", "dotted"])
def test_windows_hidden_choice_refuses(tmp_path, monkeypatch, kind):
    command = windows_files(tmp_path, monkeypatch, kind)
    with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
        resolution.validate_host_commands(parse(command), cwd=tmp_path)


# Both modeled choices can be opaque; duplicates are read once. PATHEXT and
# POSIX X_OK do not affect these choices. No Windows runtime is exercised.
def test_windows_both_native_control(tmp_path, monkeypatch):
    host(monkeypatch, tmp_path, "win32")
    for suffix in (".com", ".exe"):
        (tmp_path / ("my-server" + suffix)).write_bytes(b"MZopaque")
    opened = []
    def observe(path, *args):
        opened.append(os.fspath(path)); return os.open(path, *args)
    local_os(monkeypatch, open=observe, access=lambda *a: pytest.fail("Windows uses no X_OK"))
    resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    assert opened == [str(tmp_path / "my-server.com"), str(tmp_path / "my-server.exe")]


# Admission refuses before consent on actual CLI check, dry-run and install;
# snapshots include config, journal, launchers and grants. The execution spy is
# module-local to csk subprocess bindings and cannot poison pytest operations.
@pytest.mark.parametrize("kind", ["foreign", "interpreter", "same", "later", "quoted", "dotted"])
@pytest.mark.parametrize("argv", [("check", "app"), ("install", "app", "--dry-run"), ("install", "app")])
def test_cli_refuses_before_consent(project_cli, tmp_path, monkeypatch, capsys, kind, argv):
    root, home, declare = project_cli
    git = shutil.which("git")
    assert git
    tools = tmp_path / "tools"; tools.mkdir()
    (tools / "git").symlink_to(git)
    if kind in {"foreign", "interpreter"}:
        host(monkeypatch, tmp_path, "darwin")
        if kind == "foreign":
            executable(tmp_path / "my-server", native_header("linux"))
        else:
            interp = executable(tmp_path / "opaque", b"text")
            executable(tmp_path / "my-server", b"#!" + os.fsencode(interp) + b"\n")
        command = "my-server"
    else:
        command = windows_files(tmp_path, monkeypatch, kind)
    monkeypatch.setenv("PATH", os.environ["PATH"] + os.pathsep + str(tools))
    declare(stdio(command))
    before = _snapshot(root, home)
    terminal_views(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("refusal before consent"))
    # Existing CLI admission must not reach the shared process runner.
    import subprocess
    calls = []
    def observed(original):
        def invoke(command_args, *args, **kwargs):
            name = os.path.basename(os.fspath(command_args[0] if isinstance(command_args, (list, tuple)) else command_args))
            if name.startswith("my-server") or name == "opaque":
                calls.append(name)
                pytest.fail("no helper execution")
            return original(command_args, *args, **kwargs)
        return invoke
    proxy = SimpleNamespace(**{**vars(subprocess), "run": observed(subprocess.run), "Popen": observed(subprocess.Popen)})
    watched = []
    for module in tuple(sys.modules.values()):
        if module and getattr(module, "__name__", "").startswith("csk.") and getattr(module, "subprocess", None) is subprocess:
            monkeypatch.setattr(module, "subprocess", proxy)
            watched.append(module.__name__)
    assert watched
    with pytest.raises(pytest.fail.Exception, match="no helper execution"):
        proxy.run([command])
    assert calls == [command]
    calls.clear()
    assert cli.main(list(argv)) == 1
    out = capsys.readouterr()
    assert "mcp_command_invalid" in out.out + out.err
    assert _snapshot(root, home) == before
    assert calls == []


# Exact byte-order/fat-table controls and independently chosen fixed-field
# contradictions are exercised through admission, not a helper string mirror.
@pytest.mark.parametrize("order,bits", [("<", 32), (">", 32), ("<", 64), (">", 64)])
def test_thin_mac_orders(tmp_path, monkeypatch, order, bits):
    host(monkeypatch, tmp_path, "darwin")
    head = struct.pack(order + "7I", 0xFEEDFACF if bits == 64 else 0xFEEDFACE, 12, 0, 2, 1, 8, 0)
    executable(tmp_path / "my-server", head + (b"\0" * 4 if bits == 64 else b""))
    resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)


@pytest.mark.parametrize("count", [1, 16])
def test_fat_native_control(tmp_path, monkeypatch, count):
    host(monkeypatch, tmp_path, "darwin")
    executable(tmp_path / "my-server", fat_header(count))
    resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)


@pytest.mark.parametrize("platform,offset,value", [
    ("darwin", 12, b"\x06\0\0\0"), ("darwin", 20, b"\0" * 4),
    ("linux", 4, b"\0"), ("linux", 5, b"\0"), ("linux", 6, b"\0"),
    ("linux", 16, b"\x01\0"), ("linux", 20, b"\0" * 4), ("linux", 52, b"\0\0"),
])
def test_observed_fixed_header_contradiction(tmp_path, monkeypatch, platform, offset, value):
    host(monkeypatch, tmp_path, platform)
    head = bytearray(native_header(platform)); head[offset:offset + len(value)] = value
    executable(tmp_path / "my-server", bytes(head))
    with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)


# Open/read/fstat failures or a changed non-regular descriptor refuse without
# trying a later program; every successfully opened descriptor is closed.
@pytest.mark.parametrize("role", ["selected", "interpreter"])
@pytest.mark.parametrize("stage", ["open", "read", "fstat-error", "fstat-kind"])
def test_header_observation_failures(tmp_path, monkeypatch, role, stage):
    host(monkeypatch, tmp_path, "darwin")
    interpreter = executable(tmp_path / "opaque", native_header("darwin"))
    selected = executable(tmp_path / "my-server", b"#!" + os.fsencode(interpreter) + b"\n" if role == "interpreter" else native_header("darwin"))
    target = str(selected if role == "selected" else interpreter)
    opened = {}; created = []; closed = []
    def observed_open(path, *args):
        if str(path) == target and stage == "open":
            raise PermissionError("fixture")
        fd = os.open(path, *args); opened[fd] = str(path); created.append(fd); return fd
    def observed_read(fd, count):
        if opened[fd] == target and stage == "read":
            raise OSError("fixture")
        return os.read(fd, count)
    def observed_fstat(fd):
        if opened[fd] == target:
            if stage == "fstat-error":
                raise OSError("fixture")
            if stage == "fstat-kind":
                return SimpleNamespace(st_mode=stat.S_IFDIR)
        return os.fstat(fd)
    def observed_close(fd):
        closed.append(fd); os.close(fd)
    local_os(monkeypatch, open=observed_open, read=observed_read, fstat=observed_fstat, close=observed_close)
    with pytest.raises(mcp.McpDeclarationError) as caught:
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    assert caught.value.code == "mcp_command_invalid"
    assert "cannot be observed" in str(caught.value) if stage != "fstat-kind" else "not a regular file" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert closed == created


# Windows needs a complete searchable identity inventory too. Neither the
# version/case alias nor an unrelated opaque positive can bypass an unlistable
# directory; restoring enumeration admits the unrelated file.
@pytest.mark.parametrize("alias", [False, True])
def test_windows_unlistable_identity_inventory(tmp_path, monkeypatch, alias):
    host(monkeypatch, tmp_path, "win32")
    executable(tmp_path / "PyThOn3.12.ExE", b"MZopaque")
    if alias:
        os.link(tmp_path / "PyThOn3.12.ExE", tmp_path / "my-server.exe")
    else:
        executable(tmp_path / "my-server.exe", b"MZopaque")
    def denied(path):
        if str(path) == str(tmp_path):
            raise PermissionError("fixture")
        return os.scandir(path)
    local_os(monkeypatch, scandir=denied)
    with pytest.raises(mcp.McpDeclarationError, match="PATH identities cannot be enumerated"):
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    local_os(monkeypatch)
    if alias:
        with pytest.raises(mcp.McpDeclarationError, match="recognized evaluator"):
            resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    else:
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)


# Primary-source quote grammars differ. Semicolons inside quoted regions are
# literal; single quotes are special only to libuv. No shell or path normalize.
@pytest.mark.parametrize("path,libuv,rust", [
    ('"C:\\Tools A";C:\\later', (r"C:\Tools A", r"C:\later"), (r"C:\Tools A", r"C:\later")),
    ("'C:\\A;B';C:\\later", (r"C:\A;B", r"C:\later"), ("'C:\\A", "B'", r"C:\later")),
    ('C:\\som"e;di"r;C:\\later', ('C:\\som"e', 'di"r', r"C:\later"), (r"C:\some;dir", r"C:\later")),
    (';"";;C:\\a\\..\\b;', (r"C:\a\..\b",), (r"C:\a\..\b",)),
])
def test_windows_quote_rules(monkeypatch, path, libuv, rust):
    local_os(monkeypatch, pathsep=";")
    assert resolution._windows_path_entries(path, rust=False) == libuv
    assert resolution._windows_path_entries(path, rust=True) == rust


def test_windows_directory_components(monkeypatch):
    local_os(monkeypatch, pathsep=";", path=ntpath)
    monkeypatch.setattr(resolution, "_is_windows", lambda: True)
    result = resolution._path_directories(r'C:gate\..;D:relative;"C:\link\..\bin"', Path(r'C:\project'))
    assert result == (r'C:\project\gate\..', r'D:relative', r'C:\link\..\bin')


# Independent first-choice expectations cover suffix precedence and deduplication
# in both directions; later files are observed only if a dispatcher selects them.
@pytest.mark.parametrize("command,files,expected", [
    ("my-server", ["first/my-server.com", "first/my-server.exe"], ["first/my-server.com", "first/my-server.exe"]),
    ("my-server", ["first/my-server.com", "later/my-server.exe"], ["first/my-server.com", "later/my-server.exe"]),
    ("my-server", ["first/my-server.exe", "later/my-server.com"], ["first/my-server.exe"]),
    ("my-server.custom", ["first/my-server.custom.com", "later/my-server.custom"], ["first/my-server.custom.com", "later/my-server.custom"]),
    ("my-server.custom", ["first/my-server.custom", "later/my-server.custom.com"], ["first/my-server.custom"]),
    ("my-server.", ["first/my-server.com", "later/my-server."], ["first/my-server.com", "later/my-server."]),
    ("my-server", ["first/my-server", "first/my-server.cmd", "later/my-server.exe"], ["later/my-server.exe"]),
])
def test_windows_dispatch_choice_matrix(tmp_path, monkeypatch, command, files, expected):
    host(monkeypatch, tmp_path, "win32")
    first = tmp_path / "first"; first.mkdir()
    later = tmp_path / "later"; later.mkdir()
    for file in files:
        (tmp_path / file).write_bytes(b"MZopaque")
    monkeypatch.setenv("PATH", str(first) + os.pathsep + str(later))
    monkeypatch.setenv("PATHEXT", ".CMD;.BAT")
    opened = []
    def observed(path, *args):
        opened.append(str(Path(path).relative_to(tmp_path))); return os.open(path, *args)
    local_os(monkeypatch, open=observed)
    resolution.validate_host_commands(parse(command), cwd=tmp_path)
    assert opened == expected


# A neighboring opaque native declaration passes actual CLI admission and can
# publish after explicit consent. This prevents a reject-all CLI from satisfying
# the refusal witnesses; no fixture program is executed.
@pytest.mark.parametrize("argv", [("check", "app"), ("install", "app", "--dry-run"), ("install", "app")])
def test_cli_native_positive(project_cli, tmp_path, monkeypatch, capsys, argv):
    root, home, declare = project_cli
    git = shutil.which("git"); assert git
    tools = tmp_path / "tools"; tools.mkdir(); (tools / "git").symlink_to(git)
    platform = "win32" if os.name == "nt" else sys.platform
    host(monkeypatch, tmp_path, platform)
    executable(tmp_path / ("my-server.exe" if platform == "win32" else "my-server"), native_header(platform))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + str(tools))
    declare(stdio("my-server"))
    terminal_views(monkeypatch)
    prompts = []
    def approve(prompt):
        prompts.append(prompt); return "y"
    monkeypatch.setattr("builtins.input", approve)
    assert cli.main(list(argv)) == 0
    capsys.readouterr()
    assert bool(prompts) == (argv == ("install", "app"))
    assert (root / ".mcp.json").exists() == bool(prompts)


# Quoted SystemRoot retains realpath/component containment: an outside opaque
# file passes, an alias into the root refuses, and a prefix sibling is outside.
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("location", ["outside", "inside-link", "prefix-sibling"])
def test_windows_quoted_root_containment(tmp_path, monkeypatch, quote, location):
    host(monkeypatch, tmp_path, "win32")
    system = tmp_path / "Windows"
    own = tmp_path / ("WindowsExtra" if location == "prefix-sibling" else "tools")
    own.mkdir()
    if location == "inside-link":
        target = executable(system / "opaque.exe", b"MZopaque")
        (own / "my-server.exe").symlink_to(target)
    else:
        executable(own / "my-server.exe", b"MZopaque")
    monkeypatch.setenv("SystemRoot", quote + str(system) + quote)
    monkeypatch.setenv("PATH", '"' + str(own) + '"')
    if location == "inside-link":
        with pytest.raises(mcp.McpDeclarationError, match="base-system program"):
            resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
    else:
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)


# Every newly enforced fat-record contradiction has a nearby passing table
# control above. No payload/architecture or native kernel validity is inferred.
@pytest.mark.parametrize("offset,value", [(8, 0), (16, 0), (20, 0), (24, 32), (16, 513)])
def test_fat_record_contradiction(tmp_path, monkeypatch, offset, value):
    host(monkeypatch, tmp_path, "darwin")
    head = bytearray(fat_header(1)); head[offset:offset + 4] = value.to_bytes(4, "big")
    if value == 513:
        head[24:28] = (2).to_bytes(4, "big")
    executable(tmp_path / "my-server", bytes(head))
    with pytest.raises(mcp.McpDeclarationError, match="mcp_command_invalid"):
        resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)


# Both ELF class/byte-order layouts retain opaque executable/shared-object
# recognition; only their fixed headers are modeled, never a Linux launch.
@pytest.mark.parametrize("order,encoding", [("<", 1), (">", 2)])
@pytest.mark.parametrize("bits", [32, 64])
@pytest.mark.parametrize("kind", [2, 3])
def test_elf_layout_controls(tmp_path, monkeypatch, order, encoding, bits, kind):
    host(monkeypatch, tmp_path, "linux")
    ident = b"\x7fELF" + bytes([1 if bits == 32 else 2, encoding, 1]) + b"\0" * 9
    if bits == 32:
        head = ident + struct.pack(order + "HHIIIIIHHHHHH", kind, 40, 1, 0, 0, 0, 0, 52, 0, 0, 0, 0, 0)
    else:
        head = ident + struct.pack(order + "HHIQQQIHHHHHH", kind, 183, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0)
    executable(tmp_path / "my-server", head)
    resolution.validate_host_commands(parse("my-server"), cwd=tmp_path)
