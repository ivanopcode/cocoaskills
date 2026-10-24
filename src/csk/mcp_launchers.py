"""Portable app launcher declarations and read-only macOS identity checks.

No app is executed. Trust is a point-in-time observation, not a guarantee about
what a native agent will execute later. No shim or developer-tool dependency.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.parsers.expat import ExpatError

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


Verifier = Callable[[Path, Path, MacOSLauncher], MacOSVerification]
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}", re.ASCII)
_TEAM = re.compile(r"[A-Z0-9]{10}", re.ASCII)
_HELPER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", re.ASCII)
_WINDOWS_ROOTS = ("%LOCALAPPDATA%\\Programs\\", "%PROGRAMFILES%\\", "%PROGRAMFILES(X86)%\\")
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
    if selected != "macos":
        raise McpLauncherError("mcp_launcher_platform_unsupported", "app launcher resolution is not supported on this platform yet")
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
        checked = (verifier if verifier is not None else verify_macos)(bundle, helper, macos)
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
