"""Portable app launcher declarations and read-only platform identity checks.

No app is executed. Trust is a point-in-time observation, not a guarantee about
what a native agent will execute later. No shim or developer-tool dependency.
"""
from __future__ import annotations

import hashlib
import os
import ntpath
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal, cast
from xml.parsers.expat import ExpatError

from .mcp_authenticode import WindowsVerification, verify_windows
from .mcp_windows_folders import windows_known_folder

TrustLevel = Literal["verified", "signed-user-writable", "unverified"]


class McpLauncherError(ValueError):
    def __init__(self, code: str, detail: str, field: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.field = field


@dataclass(frozen=True)
class MacOSLauncher:
    bundle_id: str
    team_id: str
    executable: str

    @property
    def identifier(self) -> str:
        # The declaration pins the helper's signed identifier to its leaf name.
        return self.executable.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class WindowsLauncher:
    install_dir: str
    executable: str
    publisher: str
    trust: Literal["allow-unverified"] | None = None


@dataclass(frozen=True)
class LinuxLauncher:
    path: str
    trust: Literal["allow-unverified"] | None = None


@dataclass(frozen=True)
class LauncherSpec:
    macos: MacOSLauncher | None = None
    windows: WindowsLauncher | None = None
    linux: LinuxLauncher | None = None


@dataclass(frozen=True)
class ResolvedLauncher:
    path: str
    trust: TrustLevel
    publisher: str
    content_sha256: str


@dataclass(frozen=True)
class MacOSVerification:
    signature_valid: bool
    notarized: bool
    team_id: str
    bundle_id: str
    identifier: str


@dataclass(frozen=True)
class LinuxVerification:
    package_owned: bool


Verifier = Callable[..., MacOSVerification | WindowsVerification | LinuxVerification]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}", re.ASCII)
_TEAM = re.compile(r"[A-Z0-9]{10}", re.ASCII)
_HELPER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", re.ASCII)
_WINDOWS_ROOTS = ("%LOCALAPPDATA%\\", "%PROGRAMFILES%\\", "%PROGRAMFILES(X86)%\\")
_WINDOWS_ENV = {"%LOCALAPPDATA%": "LOCALAPPDATA", "%PROGRAMFILES%": "ProgramFiles",
                "%PROGRAMFILES(X86)%": "ProgramFiles(x86)"}
_LINUX_ROOTS = (
    "/usr/bin/", "/usr/local/bin/", "/opt/", "~/.local/bin/", "~/.config/",
    "${XDG_CONFIG_HOME:-~/.config}/", "${XDG_DATA_HOME:-~/.local/share}/",
)


def _invalid(field: str, detail: str) -> McpLauncherError:
    return McpLauncherError("mcp_declaration_invalid", detail, field)


def _text(raw: dict[str, object], name: str, platform: str) -> str:
    value = raw.get(name)
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(unicodedata.category(c).startswith("C") for c in value)):
        raise _invalid(f"{platform}.{name}", "expected bounded, nonempty text without control characters")
    return value


def _relative(value: str, separator: str) -> bool:
    # Expansion is restricted to the declared platform root, never a component.
    return (not any(c in value for c in '$%~:{}')
            and ("/" not in value if separator == "\\" else "\\" not in value)
            and all(part not in ("", ".", "..") for part in value.split(separator)))


def _windows_relative(value: str) -> bool:
    return (_relative(value, "\\") and not any(c in value for c in '<>"|?*')
            and all(part.rstrip(" .") == part and not re.fullmatch(
                r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                for part in value.split("\\")))


def _entry(raw: object, platform: str, allowed: set[str]) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(k, str) or k not in allowed for k in raw):
        raise _invalid(platform, "launcher entry must be an object with only documented fields")
    return raw


def _trust(raw: dict[str, object], platform: str) -> Literal["allow-unverified"] | None:
    if "trust" not in raw:
        return None
    if raw["trust"] != "allow-unverified":
        raise _invalid(f"{platform}.trust", "trust must be allow-unverified when present")
    return "allow-unverified"


def parse_launcher(raw: object) -> LauncherSpec:
    """Validate every platform, including entries for hosts not yet supported."""
    if not isinstance(raw, dict) or not raw or any(k not in {"macos", "windows", "linux"} for k in raw):
        raise _invalid("", "launcher must be a nonempty map of macos/windows/linux entries")
    macos = None
    windows = None
    linux = None
    if "macos" in raw:
        entry = _entry(raw["macos"], "macos", {"bundle_id", "team_id", "executable"})
        bundle_id = _text(entry, "bundle_id", "macos")
        team_id = _text(entry, "team_id", "macos")
        executable = _text(entry, "executable", "macos")
        if not _ID.fullmatch(bundle_id) or ".." in bundle_id:
            raise _invalid("macos.bundle_id", "bundle_id must be a portable signing identifier")
        if not _TEAM.fullmatch(team_id):
            raise _invalid("macos.team_id", "team_id must contain ten uppercase letters or digits")
        if (not executable.startswith("Contents/") or not _relative(executable, "/")
                or not _HELPER.fullmatch(executable.rsplit("/", 1)[-1])):
            raise _invalid("macos.executable", "executable must be a contained Contents/... path with a portable helper identifier")
        macos = MacOSLauncher(bundle_id, team_id, executable)
    if "windows" in raw:
        entry = _entry(raw["windows"], "windows", {"install_dir", "executable", "publisher", "trust"})
        directory = _text(entry, "install_dir", "windows")
        executable = _text(entry, "executable", "windows")
        publisher = _text(entry, "publisher", "windows")
        root = next((r for r in _WINDOWS_ROOTS if directory.upper().startswith(r.upper())), None)
        if root is None or not _windows_relative(directory[len(root):]):
            raise _invalid("windows.install_dir", "install_dir must be below a documented Windows root")
        if not _windows_relative(executable) or not executable.lower().endswith(".exe"):
            raise _invalid("windows.executable", "executable must be a contained relative .exe path")
        windows = WindowsLauncher(directory, executable, publisher, _trust(entry, "windows"))
    if "linux" in raw:
        entry = _entry(raw["linux"], "linux", {"path", "trust"})
        path = _text(entry, "path", "linux")
        root = next((r for r in _LINUX_ROOTS if path.startswith(r)), None)
        if root is None or not _relative(path[len(root):], "/"):
            raise _invalid("linux.path", "path must be below a documented Linux root")
        linux = LinuxLauncher(path, _trust(entry, "linux"))
    return LauncherSpec(macos, windows, linux)


def _platform() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


def macos_roots() -> tuple[Path, ...]:
    return (Path("/Applications"), Path.home() / "Applications", Path("/Applications/Setapp"), Path.home() / "Applications/Setapp")


def _find_bundle(spec: MacOSLauncher, roots: Sequence[Path]) -> Path:
    found: set[Path] = set()
    for root in roots:
        try:
            candidates = sorted(root.glob("*.app"))
        except OSError as exc:
            raise McpLauncherError("mcp_launcher_not_found", "application roots cannot be read") from exc
        for candidate in candidates:
            try:
                info_path = candidate / "Contents/Info.plist"
                # Do not parse arbitrary-sized files while discovering unrelated apps.
                descriptor = os.open(info_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
                        continue
                    payload = stream.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    continue
                info = plistlib.loads(payload)
                if isinstance(info, dict) and info.get("CFBundleIdentifier") == spec.bundle_id:
                    found.add(candidate.resolve(strict=True))
            except (OSError, ValueError, RuntimeError, plistlib.InvalidFileException, ExpatError):
                continue
    if not found:
        raise McpLauncherError("mcp_launcher_not_found", "no application has the declared bundle_id in the application roots")
    if len(found) != 1:
        raise McpLauncherError("mcp_launcher_ambiguous", "more than one application has the declared bundle_id")
    return next(iter(found))


def verify_macos(bundle: Path, executable: Path, spec: MacOSLauncher) -> MacOSVerification:
    """Use the OS codesign utility; strict deep validation seals nested helpers.

    Explicit requirements pin notarization, Apple's generic anchor, Team ID and
    identifiers on both the outer bundle and helper. No ad-hoc/unsigned fallback.
    The executable identifier is its declared leaf name, not the bundle ID.
    """
    for path, identifier, deep in ((bundle, spec.bundle_id, True), (executable, spec.identifier, False)):
        requirement = (f'notarized and anchor apple generic and certificate leaf[subject.OU] = "{spec.team_id}" '
                       f'and identifier "{identifier}"')
        argv = ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures"]
        if deep:
            argv.append("--deep")
        argv.extend(["-R=" + requirement, str(path)])
        try:
            result = subprocess.run(argv, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise McpLauncherError("mcp_launcher_untrusted", "macOS signature verification could not complete") from exc
        if result.returncode != 0:
            # Do not propagate tool output that can contain untrusted bundle text.
            raise McpLauncherError("mcp_launcher_untrusted", "macOS signature, seal, notarization or publisher requirement failed")
    return MacOSVerification(True, True, spec.team_id, spec.bundle_id, spec.identifier)


def resolve_launcher(
    spec: LauncherSpec, *, platform: str | None = None,
    roots: Sequence[Path] | None = None, verifier: Verifier | None = None,
) -> ResolvedLauncher:
    """Resolve without writes, server execution or consent; test seams are call arguments only."""
    selected = platform if platform is not None else _platform()
    entry = {"macos": spec.macos, "windows": spec.windows, "linux": spec.linux}.get(selected)
    if entry is None:
        raise McpLauncherError("mcp_launcher_platform_missing", "launcher has no entry for the current platform")
    if selected == "windows":
        assert spec.windows is not None
        return _resolve_windows(spec.windows, roots, verifier)
    if selected == "linux":
        assert spec.linux is not None
        return _resolve_linux(spec.linux, roots, verifier)
    assert spec.macos is not None
    macos = spec.macos
    bundle = _find_bundle(macos, roots if roots is not None else macos_roots())
    try:
        helper = (bundle / macos.executable).resolve(strict=True)
        if not helper.is_relative_to(bundle):
            raise McpLauncherError("mcp_launcher_path_invalid", "executable realpath escapes the application bundle")
        before = helper.stat()
        if not stat.S_ISREG(before.st_mode) or not os.access(helper, os.X_OK):
            raise McpLauncherError("mcp_launcher_path_invalid", "executable is not an executable regular file")
        checked = cast(MacOSVerification, (verifier if verifier is not None else verify_macos)(bundle, helper, macos))
        if (not checked.signature_valid or not checked.notarized or checked.team_id != macos.team_id
                or checked.bundle_id != macos.bundle_id or checked.identifier != macos.identifier):
            raise McpLauncherError("mcp_launcher_untrusted", "macOS signature or publisher identity did not match")
        digest = hashlib.sha256()
        with helper.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after = helper.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise McpLauncherError("mcp_launcher_untrusted", "executable changed during verification")
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, McpLauncherError):
            raise
        raise McpLauncherError("mcp_launcher_path_invalid", "application executable cannot be resolved or read") from exc
    return ResolvedLauncher(str(helper), "verified", f"macos:{checked.team_id}:{checked.bundle_id}:{checked.identifier}", digest.hexdigest())


def _path_error(detail: str) -> McpLauncherError:
    return McpLauncherError("mcp_launcher_path_invalid", detail)


def _digest_stable(path: Path, before: os.stat_result) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise _path_error("executable changed to a non-regular file")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        closed = os.fstat(stream.fileno())
    after = path.stat()
    def identity(s: os.stat_result) -> tuple[int, int, int, int, int]:
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    if not identity(before) == identity(opened) == identity(closed) == identity(after):
        raise McpLauncherError("mcp_launcher_untrusted", "executable changed during verification")
    return digest.hexdigest()


def _override_root(roots: Sequence[Path] | None) -> Path | None:
    # Call-argument-only seam for native/pure tests. No environment/config bypass.
    if roots is None:
        return None
    if len(roots) != 1:
        raise _path_error("exactly one platform root is required")
    return roots[0].resolve(strict=True)


def _windows_root_value(raw: str) -> str:
    value = raw.replace("/", "\\")
    if (not re.match(r"^[A-Za-z]:\\", value) or any(ord(c) < 32 for c in value)
            or not _windows_relative(value[3:])):
        raise _path_error("Windows known root must contain an absolute local directory")
    return ntpath.normpath(value)


def _windows_root(root: str) -> Path:
    key = _WINDOWS_ENV[root.upper()]
    try:
        known = _windows_root_value(windows_known_folder(root.upper()))
    except (OSError, ValueError, AttributeError) as exc:
        raise McpLauncherError("mcp_launcher_known_folder_unavailable", "Windows OS known folder cannot be read") from exc
    supplied = os.environ.get(key)
    if supplied is not None:
        try:
            matches = ntpath.normcase(_windows_root_value(supplied)) == ntpath.normcase(known)
        except McpLauncherError:
            matches = False
        if not matches:
            raise McpLauncherError("mcp_launcher_root_conflict", f"{key} disagrees with its Windows OS known folder")
    # Both the OS location and resolved location must be local DOS paths. A local
    # root junction onto a UNC/device namespace must not admit a remote helper.
    path = Path(known).resolve(strict=True)
    _windows_root_value(str(path))
    return path


def _windows_roots() -> dict[str, Path]:
    return {root: _windows_root(root) for root in _WINDOWS_ENV}


def _resolve_windows(entry: WindowsLauncher, roots: Sequence[Path] | None, verifier: Verifier | None) -> ResolvedLauncher:
    try:
        root = next(r for r in _WINDOWS_ROOTS if entry.install_dir.upper().startswith(r.upper()))
        selected = root[:-1].upper()
        override = _override_root(roots)
        # Explicit call-only fixture roots model one known-folder identity.
        known = {selected: override} if override is not None else _windows_roots()
        base = known[selected]
        install = base.joinpath(*entry.install_dir[len(root):].split("\\")).resolve(strict=True)
        if not install.is_relative_to(base) or not install.is_dir():
            raise _path_error("install_dir realpath escapes its Windows root")
        path = install.joinpath(*entry.executable.split("\\")).resolve(strict=True)
        if not path.is_relative_to(install):
            raise _path_error("executable realpath escapes install_dir")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise _path_error("Windows executable must be a regular file")
        checked = cast(WindowsVerification, verifier(install, path, entry) if verifier else verify_windows(path))
        if checked.signature == "unsigned":
            if entry.trust != "allow-unverified":
                raise McpLauncherError("mcp_launcher_untrusted", "unsigned launcher requires trust: allow-unverified and local consent")
            level: TrustLevel = "unverified"
            publisher = "windows:unsigned"
        elif checked.signature == "valid" and checked.publisher == entry.publisher:
            # User containment wins when known folders overlap or redirect.
            # Machine trust follows actual containment, never the placeholder.
            user = known.get("%LOCALAPPDATA%")
            if user is not None and path.is_relative_to(user):
                level = "signed-user-writable"
            elif any(path.is_relative_to(folder) for name, folder in known.items()
                     if name in {"%PROGRAMFILES%", "%PROGRAMFILES(X86)%"}):
                level = "verified"
            else:
                raise _path_error("executable is outside Windows OS known folders")
            publisher = "windows:" + checked.publisher
        else:
            raise McpLauncherError("mcp_launcher_untrusted", "Windows signature or leaf signer CN did not match")
        return ResolvedLauncher(str(path), level, publisher, _digest_stable(path, before))
    except McpLauncherError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise _path_error("Windows application executable cannot be resolved or read") from exc


def linux_uid() -> int:
    return os.getuid()


def linux_metadata(path: Path) -> os.stat_result:
    return path.stat()


def _linux_mode_error(path: PurePath, mode: int) -> McpLauncherError:
    text = str(path)
    remove = ",".join(flag for bit, flag in ((0o020, "g-w"), (0o002, "o-w")) if mode & bit)
    if text.isprintable():
        argument = shlex.quote(text)
        return _path_error(f"Linux launcher element {argument} has mode {mode:04o}; "
                           f"group/world write is forbidden; run chmod -- {remove} {argument}")
    # Escaped bytes identify the element without terminal controls. This is a
    # display name, not a shell operand: POSIX sh cannot portably decode it.
    # Escape spaces too so a name cannot echo the command cue "run chmod".
    escaped = "".join(chr(byte) if 33 <= byte < 127 and byte not in {39, 92}
                      else f"\\x{byte:02x}" for byte in os.fsencode(text))
    return _path_error(f"Linux launcher element {escaped} has mode {mode:04o}; "
                       f"group/world write is forbidden; remove {remove}; "
                       "no shell command is suggested because the name contains control characters")


def _linux_root(root: str) -> Path:
    if root.startswith("${XDG_CONFIG_HOME"):
        value = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    elif root.startswith("${XDG_DATA_HOME"):
        value = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    elif root.startswith("~/"):
        value = str(Path.home() / root[2:].rstrip("/"))
    else:
        value = root.rstrip("/")
    path = Path(value)
    if not path.is_absolute():
        raise _path_error("XDG launcher root must be absolute")
    return path.resolve(strict=True)


def _check_linux_chain(path: Path, root: Path) -> os.stat_result:
    current = path
    uid = linux_uid()
    file_info = linux_metadata(path)
    while True:
        info = linux_metadata(current)
        expected = stat.S_ISREG(info.st_mode) if current == path else stat.S_ISDIR(info.st_mode)
        if info.st_mode & 0o022:
            raise _linux_mode_error(current, stat.S_IMODE(info.st_mode))
        if not expected or info.st_uid not in {0, uid}:
            raise _path_error("Linux launcher and parents require regular files/directories owned by root or the current user")
        if current == root:
            break
        if current.parent == current:
            raise _path_error("Linux launcher root is not an ancestor")
        current = current.parent
    if not file_info.st_mode & 0o111 or not os.access(path, os.X_OK):
        raise _path_error("Linux launcher must be executable")
    return file_info


def linux_package_owned(path: Path) -> bool:
    """Ask trusted system package tools about the exact realpath, no shell.

    Fixed absolute tools, bounded runtime, and a minimal environment prevent
    relocation of the package database through inherited tool-specific options.
    dpkg -S treats its argument as a pattern: require an exact returned pathname.
    """
    for program, args in (("/usr/bin/dpkg", ["-S", "--", str(path)]),
                          ("/usr/bin/rpm", ["-qf", "--", str(path)])):
        try:
            tool = Path(program).resolve(strict=True)
            for candidate in (tool, *tool.parents):
                info = candidate.stat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise OSError("untrusted package tool")
            if not tool.is_file() or not os.access(tool, os.X_OK):
                continue
            result = subprocess.run([str(tool), *args], capture_output=True, text=True, timeout=5,
                                    check=False, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            if result.returncode == 0:
                if program.endswith("rpm") or any(line.partition(": ")[2] == str(path) for line in result.stdout.splitlines()):
                    return True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return False


def _resolve_linux(entry: LinuxLauncher, roots: Sequence[Path] | None, verifier: Verifier | None) -> ResolvedLauncher:
    try:
        root = next(r for r in _LINUX_ROOTS if entry.path.startswith(r))
        base = _override_root(roots) or _linux_root(root)
        path = (base / entry.path[len(root):]).resolve(strict=True)
        if not path.is_relative_to(base):
            raise _path_error("Linux executable realpath escapes its declared root")
        before = path.stat()
        info = _check_linux_chain(path, base)
        try:
            owned = (cast(LinuxVerification, verifier(base, path, entry)).package_owned
                     if verifier else linux_package_owned(path)) if info.st_uid == 0 else False
        except (OSError, subprocess.TimeoutExpired):
            owned = False
        level: TrustLevel = "verified" if owned else "unverified"
        if level == "unverified" and entry.trust != "allow-unverified":
            raise McpLauncherError("mcp_launcher_untrusted", "unverified Linux launcher requires trust: allow-unverified and local consent")
        return ResolvedLauncher(str(path), level, "linux:package-manager", _digest_stable(path, before))
    except McpLauncherError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise _path_error("Linux application executable cannot be resolved or read") from exc
